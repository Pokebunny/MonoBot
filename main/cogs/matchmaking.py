"""Matchmaking: one line of people who want to play, and the bot splits the
first eight into the two most balanced teams using their skill ratings.

Join means "I want to play" and Leave "I'm out", whatever is going on. With no
game on, Join queues you and the eighth join forms a match. While a group is
playing, Join puts you in line for the next open spot, and a player's Leave
sits them out of the next game; the first in line takes the spot at the next
re-team (New teams / !teams). Nobody has to know which list they belong on.

Each community has its own Lobby, and the cog routes every command and button
press to the lobby of the server it came from. discord.py runs interaction
callbacks on one event-loop thread, so no locking is needed.
"""

import asyncio
import collections
import datetime as dt
import logging
from typing import NamedTuple

import discord
from community import Community, NotACommunity, daily_time, start_daily
from discord.ext import commands, tasks
from models.matchmaking import ProposedMatch, QueuedPlayer
from services import match_embeds
from services.matchmaking import expired, next_roster, ranked_matches
from services.rating import DEFAULT_SIGMA, level_mu
from views import person_or_pick

logger = logging.getLogger(__name__)

QUEUE_TARGET = 8  # 4v4
# Waits before each retry of a formed match that failed to post (Discord 5xx).
POST_RETRY_DELAYS = (2, 5)

# How many of the most-balanced splits players can shuffle through. A full 4v4
# has 35; the top few are all near-even, past that they get lopsided.
SHUFFLE_OPTIONS = 8

# meta key holding the live lobby message pointer ("<channel_id>:<message_id>")
# -- the queue, or the teams while a game is on -- so a restart picks the
# message back up and a superseded one can still be deleted.
QUEUE_MSG_META_KEY = "queue_message"

# meta key holding the roster of the live proposal (comma-separated Discord
# ids), so New teams still works on a proposal posted before the last restart.
MATCH_ROSTER_META_KEY = "match_roster"

# meta keys holding who's sitting out the next game and the line (comma-
# separated Discord ids, the line in join order). Stored so a deploy doesn't
# lose anyone's place. The line keeps the old waitlist's key, so the waitlist
# a deploy finds becomes the start of the line.
SITTING_OUT_META_KEY = "match_sitting_out"
LINE_META_KEY = "match_waitlist"

# meta key holding when the live proposal went up (ISO, UTC). Teams are
# re-posted for every game (New teams / !teams), so a recent post means a group
# is mid-session — the stored roster alone can't say, since it outlives the
# session it belonged to.
MATCH_POSTED_AT_META_KEY = "match_posted_at"

# How recently teams must have gone up for a game to count as on: a long game
# plus the gap before someone re-teams. Past it the session is over, and
# whoever is still in line is simply the queue for the next one.
ACTIVE_GAME_MINUTES = 90

# How often queue timeouts are checked; the slack on the timeout itself.
TIMEOUT_SWEEP_SECONDS = 60


def _reset_time(config) -> dt.time | None:
    """A community's daily queue-reset time, or None if it's disabled."""
    return daily_time(config.queue_reset_time, config.queue_reset_timezone, "queue reset")


async def _lobby_or_reply(lobby_for, interaction: discord.Interaction) -> "Lobby | None":
    """The lobby of the server a button was pressed in, or None (answered)
    if that server has no community — a persistent button can outlive one
    being configured."""
    lobby = lobby_for(interaction.guild)
    if lobby is None:
        await interaction.response.send_message("MonoBot's ladder isn't set up in this server.", ephemeral=True)
    return lobby


class LobbyView(discord.ui.View):
    """Join / Leave / New teams. On the live lobby message, and on the summary
    of each game the current group plays: once a game starts nobody scrolls
    back up to the teams, but everyone sees the summary of the game they just
    finished.

    Persistent (fixed custom_ids, registered via client.add_view on cog load),
    so the buttons keep working across restarts. It holds no state: what a
    press acts on lives on the Lobby, so the one instance registered at load
    serves every message. It also takes `lobby_for(guild)` rather than a
    lobby, so each press finds the lobby of the server it came from.

    The ids are the ones the queue and proposal buttons had before the queue
    and waitlist were one line, so those messages keep working too."""

    def __init__(self, lobby_for, teams: bool = True):
        super().__init__(timeout=None)
        self.lobby_for = lobby_for
        if not teams:
            # A queue with no game on has no teams to re-split.
            self.remove_item(self.new_teams)

    def track(self, message: discord.Message | None):
        """Record a summary this view went out on, so lineup changes made
        elsewhere are redrawn on it too."""
        if message is not None and (lobby := self.lobby_for(getattr(message, "guild", None))):
            lobby.lineup_messages.append(message)

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="monobot:queue:join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if lobby := await _lobby_or_reply(self.lobby_for, interaction):
            await lobby.handle_join(interaction)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, custom_id="monobot:queue:leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if lobby := await _lobby_or_reply(self.lobby_for, interaction):
            await lobby.handle_leave(interaction)

    @discord.ui.button(
        label="New teams", style=discord.ButtonStyle.primary, emoji="🔀", custom_id="monobot:match:newteams"
    )
    async def new_teams(self, interaction: discord.Interaction, button: discord.ui.Button):
        if lobby := await _lobby_or_reply(self.lobby_for, interaction):
            await lobby.handle_new_teams(interaction)


class LegacyLineupView(discord.ui.View):
    """The Sit out / Waitlist buttons that proposals and summaries carried
    before the queue and waitlist became one line. Those messages are still in
    chat, so their buttons keep working: Sit out is Leave, Waitlist is Join."""

    def __init__(self, lobby_for):
        super().__init__(timeout=None)
        self.lobby_for = lobby_for

    async def _join(self, interaction: discord.Interaction):
        if lobby := await _lobby_or_reply(self.lobby_for, interaction):
            await lobby.handle_join(interaction)

    async def _leave(self, interaction: discord.Interaction):
        if lobby := await _lobby_or_reply(self.lobby_for, interaction):
            await lobby.handle_leave(interaction)

    @discord.ui.button(label="Sit out", custom_id="monobot:match:sitout")
    async def match_sit_out(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._leave(interaction)

    @discord.ui.button(label="Waitlist", custom_id="monobot:match:waitlist")
    async def match_waitlist(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._join(interaction)

    @discord.ui.button(label="Sit out", custom_id="monobot:next:sitout")
    async def next_sit_out(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._leave(interaction)

    @discord.ui.button(label="Waitlist", custom_id="monobot:next:waitlist")
    async def next_waitlist(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._join(interaction)

    @discord.ui.button(label="New teams", custom_id="monobot:next:newteams")
    async def next_new_teams(self, interaction: discord.Interaction, button: discord.ui.Button):
        if lobby := await _lobby_or_reply(self.lobby_for, interaction):
            await lobby.handle_new_teams(interaction)


# The levels a new player picks from (rating.SKILL_LEVEL_MU): key, button
# label, what it means in SC2 ladder terms.
SKILL_LEVELS = [
    ("beginner", "Beginner", "below Diamond"),
    ("mid", "Mid", "Diamond to Master"),
    ("top", "Top", "Grandmaster"),
]
LEVEL_PROMPT = (
    "Welcome! You haven't played a monobattle here yet — what's your usual SC2 ladder level? "
    "Your first game's rating starts from it, so the teams come out fairer."
)


def _level_label(level: str) -> str:
    return next(f"**{label}** ({meaning})" for key, label, meaning in SKILL_LEVELS if key == level)


class SkillLevelView(discord.ui.View):
    """The level buttons, for one player: shown privately on their first
    Join (until they answer, every Join asks again). Not persistent."""

    def __init__(self, lobby_for, owner_id: int):
        super().__init__(timeout=600)
        self.lobby_for = lobby_for
        self.owner_id = owner_id
        for key, label, meaning in SKILL_LEVELS:
            button = discord.ui.Button(label=f"{label} — {meaning}", style=discord.ButtonStyle.primary)
            button.callback = self._picker(key)
            self.add_item(button)

    def _picker(self, level: str):
        async def pick(interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.response.send_message("These buttons are someone else's.", ephemeral=True)
                return
            if lobby := await _lobby_or_reply(self.lobby_for, interaction):
                await interaction.response.edit_message(content=lobby.set_level(interaction.user, level), view=None)

        return pick


class Outcome(NamedTuple):
    """What a Join or Leave did."""

    reply: str
    changed: bool = False  # the lineup moved, so the lobby needs redrawing
    # A plain queue change with no game on: the queue message itself shows
    # it, so a press on that message just redraws it rather than replying.
    quiet: bool = False
    roster: list | None = None  # the eight a full queue just handed over


def _say(label: str | None, you: str, them: str) -> str:
    """`you` for the player who pressed a button themself; `them`, with
    {name} filled in, when a command acts for someone else (label)."""
    return you if label is None else them.format(name=f"**{label}**")


class Lobby:
    """One community's matchmaking: the line, the live teams, and who's
    sitting out. `lobby_for` is what views it posts are given, so their
    buttons find this lobby again (after a restart too).

    The line is everyone who wants to play and isn't in the current game, in
    the order they joined. With no game on it's the queue, and teams form when
    eight are in it. While a game is on, it's who takes the next open spot.
    One list either way, so a session ending (no teams for
    ACTIVE_GAME_MINUTES) needs no hand-over: whoever's waiting is queued."""

    def __init__(self, client, community: Community, lobby_for):
        self.client = client
        self.community = community
        self.config = community.config
        self.store = community.store
        self.ratings = community.ratings
        self.lobby_for = lobby_for
        self.line: list[str] = self._stored_ids(LINE_META_KEY)
        self.sitting_out: list[str] = self._stored_ids(SITTING_OUT_META_KEY)
        # Users seen pressing a button or added by command, so forming a match
        # needn't look them up; anyone else is found in the server.
        self.members: dict[str, discord.abc.User] = {}
        # When each person in the line last pressed Join; while no game is on
        # they're dropped once it's queue_timeout_minutes old. A restored line
        # starts its clocks at the restart.
        now = discord.utils.utcnow()
        self.joined_at: dict[str, dt.datetime] = {uid: now for uid in self.line}
        # The one live lobby message: the queue, or the teams while a game is on.
        self.message: discord.Message | None = None
        # The last roster to get teams, so New teams / !teams can re-split it.
        # Users, not QueuedPlayers: ratings are looked up fresh each time so
        # re-teaming picks up recent games.
        self.last_roster: list[discord.abc.User] = []
        # The ranked splits of that roster, which New teams cycles through
        # until a game is stored (games_at_post). Lost on restart, after which
        # New teams re-balances instead.
        self.options: list[ProposedMatch] = []
        self.option_index = 0
        self.games_at_post = 0
        # The latest few summaries carrying the lobby buttons, whose lineup
        # fields are redrawn when the lineup changes.
        self.lineup_messages: collections.deque[discord.Message] = collections.deque(maxlen=3)

    # -- state ---------------------------------------------------------------

    def _stored_ids(self, key: str) -> list[str]:
        return [i for i in (self.store.get_meta(key) or "").split(",") if i]

    def save_lineup(self):
        self.store.set_meta(SITTING_OUT_META_KEY, ",".join(self.sitting_out))
        self.store.set_meta(LINE_META_KEY, ",".join(self.line))

    def stored_roster_ids(self) -> list[str]:
        """Discord ids of the live teams' roster, as last posted."""
        return self._stored_ids(MATCH_ROSTER_META_KEY)

    def game_active(self) -> bool:
        """Whether a group is mid-session: teams went up within the last
        ACTIVE_GAME_MINUTES."""
        posted = self.store.get_meta(MATCH_POSTED_AT_META_KEY)
        if not posted or not self.stored_roster_ids():
            return False
        age = discord.utils.utcnow() - dt.datetime.fromisoformat(posted)
        return age < dt.timedelta(minutes=ACTIVE_GAME_MINUTES)

    def playing(self, uid: str) -> bool:
        return self.game_active() and uid in self.stored_roster_ids()

    def renew(self, uid: str):
        """Start (or restart) someone's queue timeout."""
        self.joined_at[uid] = discord.utils.utcnow()

    def _user(self, uid: str, guild: discord.Guild | None) -> discord.abc.User | None:
        if uid in self.members:
            return self.members[uid]
        return guild.get_member(int(uid)) if guild is not None else None

    def resolve_roster(self, guild: discord.Guild | None) -> list[discord.abc.User]:
        """The stored roster as users. Empty if it can't be fully resolved --
        re-teaming a partial roster would silently drop players."""
        users = [self._user(uid, guild) for uid in self.stored_roster_ids()]
        return users if users and all(u is not None for u in users) else []

    # -- the line ------------------------------------------------------------

    def join(self, member, guild: discord.Guild | None, label: str | None = None) -> Outcome:
        """Someone wants to play: back in if they were sitting out, otherwise
        into the line. `label` names them when a command acts for them."""
        uid = str(member.id)
        self.members[uid] = member
        # An unlinked player would queue on the new-player default and quietly
        # skew the balance, so say so rather than adding them.
        if not self.store.sc2_names_for(uid):
            return Outcome(
                _say(
                    label,
                    "You need to link your SC2 name before you can play. Run `!link <your SC2 name>` first.",
                    "{name} hasn't linked an SC2 name yet — they need to run `!link <their SC2 name>` first.",
                )
            )
        if self.playing(uid):
            if uid not in self.sitting_out:
                return Outcome(_say(label, "You're already in this game.", "{name} is already in this game."))
            self.sitting_out.remove(uid)
            self.save_lineup()
            return Outcome(
                _say(label, "You're back in for the next game.", "{name} is back in for the next game."), changed=True
            )
        if uid in self.line:
            self.renew(uid)
            if self.game_active():
                place = self.line.index(uid) + 1
                return Outcome(
                    _say(label, f"You're already #{place} in line.", f"{{name}} is already #{place} in line.")
                )
            return Outcome(
                _say(
                    label,
                    f"You're already in the queue — your timer's restarted, you'll stay in for another "
                    f"{self.config.queue_timeout_minutes} minutes.",
                    "{name} is already in the queue — restarted their timer.",
                )
            )
        self.line.append(uid)
        self.renew(uid)
        self.save_lineup()
        if self.game_active():
            place = len(self.line)
            return Outcome(
                _say(
                    label,
                    f"You're #{place} in line — you'll be pinged when a spot opens. Press **Leave** to drop out.",
                    f"{{name}} is #{place} in line — they'll be pinged when a spot opens.",
                ),
                changed=True,
            )
        roster = self._take_queue(guild) if len(self.line) >= QUEUE_TARGET else None
        return Outcome(
            _say(label, "You're in the queue.", "Added {name} to the queue."), changed=True, quiet=True, roster=roster
        )

    def leave(self, uid: str, label: str | None = None) -> Outcome:
        """Someone's out: off the line, or sitting out the next game."""
        if uid in self.line:
            self.line.remove(uid)
            self.joined_at.pop(uid, None)
            self.save_lineup()
            where = "line" if self.game_active() else "queue"
            return Outcome(
                _say(label, f"You've left the {where}.", f"Took {{name}} out of the {where}."),
                changed=True,
                quiet=not self.game_active(),
            )
        if self.playing(uid):
            if uid in self.sitting_out:
                return Outcome(
                    _say(
                        label,
                        "You're already sitting out the next game — press **Join** to come back.",
                        "{name} is already sitting out the next game.",
                    )
                )
            self.sitting_out.append(uid)
            self.save_lineup()
            return Outcome(
                _say(
                    label,
                    "You'll sit out the next game; the first person in line takes your spot. "
                    "Press **Join** to change your mind.",
                    "{name} will sit out the next game; the first person in line takes their spot.",
                ),
                changed=True,
            )
        return Outcome(_say(label, "You're not in the queue or the game.", "{name} isn't in the queue or the game."))

    def _take_queue(self, guild: discord.Guild | None) -> list[discord.abc.User] | None:
        """The first eight in line, taken out of it, or None if fewer than
        eight can be found (anyone who left the server is dropped)."""
        users = []
        for uid in list(self.line):
            user = self._user(uid, guild)
            if user is None:
                self.line.remove(uid)
                continue
            users.append(user)
            if len(users) == QUEUE_TARGET:
                break
        if len(users) < QUEUE_TARGET:
            self.save_lineup()
            return None
        taken = {str(u.id) for u in users}
        self.line = [uid for uid in self.line if uid not in taken]
        self.save_lineup()
        return users

    def lineup_for_next(
        self, guild: discord.Guild | None, users: list[discord.abc.User]
    ) -> tuple[list[discord.abc.User], list[discord.abc.User]] | str:
        """`users` with sit-outs swapped for the first in line: (roster,
        promoted), or a message saying why the swap can't happen yet. People
        in line who've left the server are skipped, not counted."""
        found = {str(u.id): u for u in users}
        for uid in self.line:
            if uid not in found and (user := self._user(uid, guild)) is not None:
                found[uid] = user
        waiting = [uid for uid in self.line if uid in found]
        roster, promoted, short = next_roster([str(u.id) for u in users], self.sitting_out, waiting)
        if short:
            return (
                f"{len(self.sitting_out)} sitting out but only {len(promoted)} in line — need {short} more. "
                "Press **Join** to take a spot; anyone sitting out can press **Join** to stay in."
            )
        return [found[uid] for uid in roster], [found[uid] for uid in promoted]

    # -- timers and resets ---------------------------------------------------

    # A queue that sat unfilled overnight is stale: people who joined, never
    # got a game and forgot to leave make the count look healthier than it is.
    # Wiped once a day in the small hours (the community's queue_reset_time).
    async def daily_reset(self):
        had = bool(self.line or self.sitting_out)
        self.line.clear()
        self.sitting_out.clear()
        self.joined_at.clear()
        self.save_lineup()
        if had:
            logger.info("Daily queue reset (community %s)", self.community.guild_id)
            # Silent: the lobby message just redraws, no new post.
            await self._refresh()

    # The daily reset catches a queue forgotten overnight; this catches one
    # player who wandered off and would otherwise be pinged into a match they
    # aren't there for. Only while no game is on: waiting in line through a
    # game is the point of the line, so its clocks start when the session
    # ends. Silent on purpose: a reminder before removal was tried and found
    # too noisy, so the queue message states the rule instead and pressing
    # Join again restarts the clock.
    async def sweep_timeouts(self):
        if self.game_active():
            return
        now = discord.utils.utcnow()
        posted = self.store.get_meta(MATCH_POSTED_AT_META_KEY)
        session_end = dt.datetime.fromisoformat(posted) + dt.timedelta(minutes=ACTIVE_GAME_MINUTES) if posted else None
        starts = {}
        for uid in self.line:
            start = self.joined_at.setdefault(uid, now)
            starts[uid] = max(start, session_end) if session_end else start
        gone = expired(starts, now, dt.timedelta(minutes=self.config.queue_timeout_minutes))
        for uid in gone:
            self.line.remove(uid)
            del self.joined_at[uid]
            logger.info("Queue timeout: removed %s", uid)
        if gone:
            self.save_lineup()
            await self._refresh()

    # -- rating lookup ---------------------------------------------------

    def _queued_player(self, user: discord.abc.User) -> QueuedPlayer:
        """Build a QueuedPlayer, rated by the user's bound SC2 account with the
        most games. A season's seed counts until they've played in it, so a
        soft reset balances on carried-over skill from the first game. Users
        who haven't played yet start from the level they reported on Join,
        or the new-player default."""
        book = self.ratings.book()
        best = None
        for handle in self.store.handles_for(str(user.id)):
            rating = book.standing_for(handle)  # follows account merges
            if rating is not None and (best is None or rating.games > best.games):
                best = rating
        if best is not None and best.name:
            return QueuedPlayer(
                discord_id=str(user.id),
                display_name=user.display_name,
                sc2_name=best.name,
                mu=best.mu,
                sigma=best.sigma,
            )
        return QueuedPlayer(
            discord_id=str(user.id),
            display_name=user.display_name,
            sc2_name=None,
            mu=level_mu(self.store.skill_level(str(user.id))),
            sigma=DEFAULT_SIGMA,
        )

    # -- the lobby message -----------------------------------------------

    def meeting_point(self) -> str | None:
        return match_embeds.meeting_point(self.config.sc2_chat_channel, self.config.sc2_region)

    def lineup_embed(self, embed: discord.Embed) -> discord.Embed:
        """`embed` with who's sitting out and who's in line under it. With no
        game on the line is the queue, which the queue message lists itself."""
        return match_embeds.with_lineup_changes(embed, self.sitting_out, self.line if self.game_active() else [])

    def _embed(self) -> discord.Embed:
        """The lobby as it stands: the queue, or the teams while a game is on."""
        if not self.game_active():
            entries = [(uid, self.has_played(uid)) for uid in self.line]
            return match_embeds.queue_status(
                entries, QUEUE_TARGET, self.config.queue_timeout_minutes, self.meeting_point()
            )
        if self.options:
            base = match_embeds.proposed_match(
                self.options[self.option_index], self.option_index, len(self.options), self.meeting_point()
            )
        elif self.message is not None and self.message.embeds:
            base = self.message.embeds[0]  # a restart lost the split; keep the teams already shown
        else:
            base = match_embeds.current_lineup(self.stored_roster_ids())
        return self.lineup_embed(base)

    def _view(self) -> LobbyView:
        return LobbyView(self.lobby_for, teams=self.game_active())

    def _is_lobby_message(self, message) -> bool:
        return self.message is not None and message is not None and message.id == self.message.id

    async def _refresh(self, skip: discord.Message | None = None):
        """Redraw the lobby wherever it shows -- the lobby message and the
        lineup fields on the latest summaries -- except `skip`, which the
        caller has just drawn itself. A summary's embed is read back off its
        message, so it keeps its own fields."""
        if self.message is not None and not (skip is not None and self.message.id == skip.id):
            try:
                await self.message.edit(embed=self._embed(), view=self._view())
            except discord.HTTPException:
                self.message = None
                self.store.set_meta(QUEUE_MSG_META_KEY, "")
        for message in self.lineup_messages:
            if not message.embeds or (skip is not None and message.id == skip.id):
                continue
            try:
                await message.edit(embed=self.lineup_embed(message.embeds[0]))
            except discord.HTTPException:
                pass

    async def _show(self, message: discord.Message):
        """Make `message` the one live lobby message and delete the one before,
        so nobody acts on a superseded queue or split. The pointer lives in
        the DB, so a message from before a restart goes too."""
        previous, old_ref = self.message, self.store.get_meta(QUEUE_MSG_META_KEY) or ""
        new_ref = f"{message.channel.id}:{message.id}"
        self.message = message
        self.store.set_meta(QUEUE_MSG_META_KEY, new_ref)
        if not old_ref or old_ref == new_ref:
            return
        if previous is not None and f"{previous.channel.id}:{previous.id}" == old_ref:
            try:
                await previous.delete()
            except discord.HTTPException:
                pass
        elif (old := await self._fetch_message_ref(old_ref)) is not None:
            try:
                await old.delete()
            except discord.HTTPException:
                pass

    async def _fetch_message_ref(self, ref: str) -> discord.Message | None:
        """The message a stored "<channel_id>:<message_id>" pointer names, or
        None if it's gone or unreachable."""
        try:
            channel_id, message_id = (int(part) for part in ref.split(":"))
        except ValueError:
            return None
        channel = self.client.get_channel(channel_id)
        try:
            if channel is None:
                channel = await self.client.fetch_channel(channel_id)
            return await channel.fetch_message(message_id)
        except discord.HTTPException:
            return None

    async def restore_message(self):
        """After a restart, pick the lobby message back up so it stays live and
        current. On a gateway reconnect the live one is still in hand."""
        if self.message is not None:
            return
        ref = self.store.get_meta(QUEUE_MSG_META_KEY)
        if not ref:
            return
        message = await self._fetch_message_ref(ref)
        if message is None:
            self.store.set_meta(QUEUE_MSG_META_KEY, "")
            return
        self.message = message
        await self._refresh()

    async def open_queue(self, ctx):
        """!queue: the lobby at the bottom of chat. Moves the live message
        rather than opening a second one. No role ping -- re-opening it
        mid-session is routine, and pinging the whole role each time is
        noise. Whoever wants the community called in can @ the role."""
        await self._show(await ctx.send(embed=self._embed(), view=self._view()))

    # -- buttons ---------------------------------------------------------

    async def _answer(self, interaction: discord.Interaction, outcome: Outcome):
        """Answer a press, before anything slow: Discord drops a press that
        isn't answered within 3 seconds (that once lost the 8th join's press
        after the queue was already emptied). A plain queue change pressed on
        the queue message just redraws it; anything else replies privately
        and then redraws the lobby."""
        if outcome.quiet and self._is_lobby_message(interaction.message):
            await interaction.response.edit_message(embed=self._embed(), view=self._view())
            await self._refresh(skip=interaction.message)
            return
        await interaction.response.send_message(outcome.reply, ephemeral=True)
        if outcome.changed:
            await self._refresh()

    async def handle_join(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        outcome = self.join(interaction.user, interaction.guild)
        await self._answer(interaction, outcome)
        if outcome.changed and self.needs_level(uid):
            view = SkillLevelView(self.lobby_for, interaction.user.id)
            await interaction.followup.send(LEVEL_PROMPT, view=view, ephemeral=True)
        if outcome.roster:
            await self.post_formed_match(interaction.channel, outcome.roster)

    async def handle_leave(self, interaction: discord.Interaction):
        await self._answer(interaction, self.leave(str(interaction.user.id)))

    async def handle_new_teams(self, interaction: discord.Interaction):
        """New teams does whichever of two things the moment calls for:

        - No games since the teams went up, and nobody sitting out: cycle the
          splits that were ranked then -- same ratings, different split.
        - Otherwise: re-split from the players' *current* ratings, so the
          replay that just went up counts, swapping sit-outs for the first in
          line. Groups play a couple of games before re-teaming, and by then
          the ranked options no longer reflect where anyone stands.

        Asking which one they meant would be a worse button: the stale options
        are never what someone wants after a game, and before one there is
        nothing to re-balance from. A restart loses the options, so New teams
        then goes straight to re-balancing. Only a player in the match may
        press it."""
        if str(interaction.user.id) not in self.stored_roster_ids():
            await interaction.response.send_message("Only a player in this match can re-team.", ephemeral=True)
            return
        fresh = self.options and self.store.match_count() == self.games_at_post and not self.sitting_out
        if fresh and len(self.options) > 1:
            self.option_index = (self.option_index + 1) % len(self.options)
            if self._is_lobby_message(interaction.message):
                await interaction.response.edit_message(embed=self._embed(), view=self._view())
            else:
                await interaction.response.defer()
                await self._show(await interaction.channel.send(embed=self._embed(), view=self._view()))
            return
        if fresh:
            await interaction.response.send_message(
                "This roster only splits one sensible way, and no games have been played since.", ephemeral=True
            )
            return
        users = self.last_roster or self.resolve_roster(interaction.guild)
        if not users:
            await interaction.response.send_message(
                "I've lost this match's roster since my last restart — run `!queue` and Join to start again.",
                ephemeral=True,
            )
            return
        lineup = self.lineup_for_next(interaction.guild, users)
        if isinstance(lineup, str):
            await interaction.response.send_message(lineup, ephemeral=True)
            return
        roster, promoted = lineup
        await interaction.response.defer()
        await self.post_match(interaction.channel, roster, promoted=promoted)

    # -- commands --------------------------------------------------------

    async def _member_for(self, ctx, query: str, on_pick):
        """A guild member from a typed name, resolved the way every command
        resolves names (views.person_or_pick). No partial matching — queueing
        the wrong person is worse than being told to type the whole name.

        None when the caller has already been answered: nothing matched, the
        name is shared and a picker went out, or the person isn't reachable."""
        people = await person_or_pick(ctx, self.store, query, on_pick)
        if people is None:
            return None
        person = people[0]
        member = await self._member_of(ctx, person)
        if member is not None:
            return member
        if person.discord_id:
            await ctx.send(f"**{person.sc2_name}** is linked, but isn't in this server.")
        else:
            await ctx.send(
                f"**{person.sc2_name}** hasn't linked a Discord account yet — "
                "they need to run `!link <their SC2 name>` before they can queue."
            )
        return None

    async def _member_of(self, ctx, person) -> discord.Member | None:
        if person.discord_id is None or ctx.guild is None:
            return None
        return ctx.guild.get_member(int(person.discord_id))

    async def _for_someone(self, ctx, player: str, act):
        """Run `act(member, label)` for a typed name, picker included; it
        returns the Outcome, which is said in the channel. Open to everyone:
        it only does what that person's own Join or Leave would."""

        async def picked(interaction, person):
            member = await self._member_of(ctx, person)
            if member is None:
                await interaction.response.edit_message(
                    content=f"**{person.sc2_name}** isn't in this server.", view=None
                )
                return
            outcome = act(member, person.sc2_name)
            await interaction.response.edit_message(content=outcome.reply, view=None)
            await self._after_command(ctx, outcome)

        member = await self._member_for(ctx, player, picked)
        if member is None:
            return
        outcome = act(member, member.display_name)
        await ctx.send(outcome.reply)
        await self._after_command(ctx, outcome)

    async def _after_command(self, ctx, outcome: Outcome):
        if outcome.changed:
            await self._refresh()
        if outcome.roster:
            await self.post_formed_match(ctx.channel, outcome.roster)

    async def add(self, ctx, player: str):
        """!add: Join for someone else."""
        await self._for_someone(ctx, player, lambda member, label: self.join(member, ctx.guild, label))

    async def bump(self, ctx, player: str):
        """!bump: Leave for someone else."""
        await self._for_someone(ctx, player, lambda member, label: self.leave(str(member.id), label))

    async def teams(self, ctx):
        """!teams: re-split the current roster from live ratings, swapping
        sit-outs for the first in line -- New teams without the cycling."""
        users = self.last_roster or self.resolve_roster(ctx.guild)
        if not users:
            await ctx.send("No recent match to re-team — run `!queue` to start one.")
            return
        lineup = self.lineup_for_next(ctx.guild, users)
        if isinstance(lineup, str):
            await ctx.send(lineup)
            return
        roster, promoted = lineup
        if len(roster) < 2 or len(roster) % 2 != 0:
            await ctx.send(f"Need an even number of players, got {len(roster)}.")
            return
        await self.post_match(ctx.channel, roster, promoted=promoted)

    async def clear_queue(self, ctx):
        self.line.clear()
        self.joined_at.clear()
        self.save_lineup()
        await self._refresh()
        await ctx.send("Queue cleared.")

    # -- self-reported level ---------------------------------------------

    def has_played(self, uid: str) -> bool:
        """Whether a game of theirs is stored here: a link binds its account
        handle the first time the name shows up in an upload."""
        return bool(self.store.handles_for(uid))

    def needs_level(self, uid: str) -> bool:
        return not self.has_played(uid) and self.store.skill_level(uid) is None

    def set_level(self, user: discord.abc.User, level: str) -> str:
        """Record a level, and say what happened. Refused once they've played:
        their rating then comes from their games, and changing the start
        would rewrite every rating since."""
        uid = str(user.id)
        if self.has_played(uid):
            return "You've played here already, so your rating comes from your games now."
        self.store.set_skill_level(uid, level)
        return f"Got it — you'll start as {_level_label(level)}."

    # -- posting teams ---------------------------------------------------

    async def post_formed_match(self, channel: discord.abc.Messageable, users: list[discord.abc.User]):
        """Announce the match a full queue just formed. The eight are already
        out of the line, so a failure here would otherwise drop them on the
        floor: the roster is kept first, and if posting fails anyway the
        channel is told to run !teams, which re-splits it."""
        self.last_roster = list(users)
        self.store.set_meta(MATCH_ROSTER_META_KEY, ",".join(str(u.id) for u in users))
        for delay in (0, *POST_RETRY_DELAYS):
            await asyncio.sleep(delay)
            try:
                await self.post_match(channel, users, announce=True)
                return
            except Exception:
                logger.exception("Posting the formed match failed")
        names = ", ".join(u.display_name for u in users)
        await channel.send(f"The queue filled ({names}) but the teams didn't post — run `!teams` to split them.")

    async def post_match(
        self,
        channel: discord.abc.Messageable,
        users: list[discord.abc.User],
        announce: bool = False,
        promoted: list[discord.abc.User] = (),
    ):
        """Balance `users` from their current ratings and post the teams as the
        lobby message at the bottom of the channel, superseding the last one.

        The ratings are read here rather than passed in, so every route into
        this (queue filling, New teams, !teams) reflects games played since
        the last split. `announce` mentions the players — on for a freshly
        formed match, off for a re-team, where everyone is already watching
        and eight pings per re-roll would be spam. `promoted` are people from
        the line who just took a spot; they're pinged even on a re-team,
        since they aren't watching the way the players are.

        Starts a fresh lineup: sit-outs belonged to the old roster, and anyone
        now playing comes out of the line."""
        players = [self._queued_player(u) for u in users]
        self.options = ranked_matches(players, limit=SHUFFLE_OPTIONS)
        self.option_index = 0
        self.games_at_post = self.store.match_count()
        self.last_roster = list(users)
        self.store.set_meta(MATCH_ROSTER_META_KEY, ",".join(str(u.id) for u in users))
        self.store.set_meta(MATCH_POSTED_AT_META_KEY, discord.utils.utcnow().isoformat())
        roster_ids = {str(u.id) for u in users}
        lineup_changed = bool(self.sitting_out) or any(uid in roster_ids for uid in self.line)
        self.sitting_out.clear()
        self.line = [uid for uid in self.line if uid not in roster_ids]
        for uid in roster_ids:
            self.joined_at.pop(uid, None)
        self.save_lineup()
        pinged = list(users) if announce else list(promoted)
        content = None
        if announce:
            content = " ".join(f"<@{u.id}>" for u in users) + " — your match is ready!"
        elif promoted:
            content = " ".join(f"<@{u.id}>" for u in promoted) + " — a spot opened up, you're in!"
        message = await channel.send(
            content=content,
            embed=self._embed(),
            view=self._view(),
            allowed_mentions=discord.AllowedMentions(users=pinged),
        )
        # Teams go up first and the tidying after, all at once: each of these
        # is a Discord round-trip, and run one by one ahead of the send they
        # made a re-team look frozen for seconds.
        cleanup = [self._show(message)]
        if lineup_changed:
            cleanup.append(self._refresh(skip=message))
        await asyncio.gather(*cleanup)

    def next_game_view(self, handles: set[str]) -> LobbyView | None:
        """Lobby buttons for the summary of a game with these players' toon
        handles, if it was the current group's game — at least half the live
        roster played in it. None for anyone else's game, where handing over
        spots in this roster would make no sense."""
        roster = self.stored_roster_ids()
        if not roster:
            return None
        played = sum(1 for uid in roster if handles & set(self.store.handles_for(uid)))
        return LobbyView(self.lobby_for) if played * 2 >= len(roster) else None


class Matchmaking(commands.Cog):
    """Routes each command and button press to its server's Lobby."""

    def __init__(self, client):
        self.client = client
        self.lobbies: dict[Community, Lobby] = {}
        self.daily_resets: list[tasks.Loop] = []

    def lobby_for(self, where) -> Lobby | None:
        """The lobby of a Community, or of the one a guild belongs to (None if
        it has none). Made on first use."""
        community = where if isinstance(where, Community) else self.client.communities.for_guild(where)
        if community is None:
            return None
        if community not in self.lobbies:
            self.lobbies[community] = Lobby(self.client, community, self.lobby_for)
        return self.lobbies[community]

    async def cog_check(self, ctx) -> bool:
        if self.lobby_for(ctx.guild) is None:
            raise NotACommunity(ctx)
        return True

    async def cog_load(self):
        # Register the persistent views so the buttons on lobby messages and
        # summaries from before the last restart still dispatch here.
        self.client.add_view(LobbyView(self.lobby_for))
        self.client.add_view(LegacyLineupView(self.lobby_for))
        self.daily_resets = start_daily(
            self.client, _reset_time, lambda community: self.lobby_for(community).daily_reset(), "Daily queue reset"
        )
        if any(c.config.queue_timeout_minutes for c in self.client.communities.all()):
            self.timeout_sweep.start()

    async def cog_unload(self):
        for loop in self.daily_resets:
            loop.cancel()
        self.timeout_sweep.cancel()

    @tasks.loop(seconds=TIMEOUT_SWEEP_SECONDS)
    async def timeout_sweep(self):
        for community in self.client.communities.all():
            if community.config.queue_timeout_minutes:
                await self.lobby_for(community).sweep_timeouts()

    @timeout_sweep.before_loop
    async def before_timeout_sweep(self):
        await self.client.wait_until_ready()

    @timeout_sweep.error
    async def timeout_sweep_error(self, error):
        # A loop task dies on an unhandled exception; log it and keep sweeping.
        logger.exception("Queue timeout sweep failed", exc_info=error)
        self.timeout_sweep.restart()

    @commands.Cog.listener()
    async def on_ready(self):
        # The line and lineup survive a restart, so the lobby message does too.
        for community in self.client.communities.all():
            await self.lobby_for(community).restore_message()

    @commands.hybrid_command(help="show the matchmaking lobby — the queue, or the teams and who's waiting")
    @commands.cooldown(1, 30, commands.BucketType.channel)
    async def queue(self, ctx):
        await self.lobby_for(ctx.guild).open_queue(ctx)

    @commands.hybrid_command(
        aliases=["remove"], help="Leave for someone: out of the queue, or sitting out the next game"
    )
    async def bump(self, ctx, *, player: str):
        await self.lobby_for(ctx.guild).bump(ctx, player)

    @commands.hybrid_command(help="re-split the current match's players into fresh teams")
    @commands.cooldown(1, 10, commands.BucketType.channel)
    async def teams(self, ctx):
        await self.lobby_for(ctx.guild).teams(ctx)

    @commands.hybrid_command(help="Join for someone: into the queue, or in line for a spot while a game's on")
    async def add(self, ctx, *, player: str):
        await self.lobby_for(ctx.guild).add(ctx, player)

    @commands.hybrid_command(help="clear the matchmaking queue")
    async def clearqueue(self, ctx):
        await self.lobby_for(ctx.guild).clear_queue(ctx)


async def setup(client):
    await client.add_cog(Matchmaking(client))
