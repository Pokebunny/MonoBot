"""Matchmaking: one line of people who want to play; the first eight are split
into the most balanced teams.

Join means "I want to play", Leave "I'm out". With no game on, the eighth Join
forms a match. During a session Join takes the next open spot and Leave sits
out the next game, swapped at the next re-team. A session ends on End session
or after ACTIVE_GAME_MINUTES without a re-team, and the line becomes the queue.
One Lobby per community; interaction callbacks share one thread, so no locks.
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

# Meta keys, so a restart keeps the lobby. Live lobby message: "<channel>:<message>".
QUEUE_MSG_META_KEY = "queue_message"

# Comma-separated Discord ids.
MATCH_ROSTER_META_KEY = "match_roster"

# The line keeps the old waitlist's key.
SITTING_OUT_META_KEY = "match_sitting_out"
LINE_META_KEY = "match_waitlist"

# ISO UTC; a recent post means a session is on.
MATCH_POSTED_AT_META_KEY = "match_posted_at"

# A long game plus the gap before a re-team.
ACTIVE_GAME_MINUTES = 90

# How often queue timeouts are checked; the slack on the timeout itself.
TIMEOUT_SWEEP_SECONDS = 60


def _reset_time(config) -> dt.time | None:
    """A community's daily queue-reset time, or None if it's disabled."""
    return daily_time(config.queue_reset_time, config.queue_reset_timezone, "queue reset")


async def _lobby_or_reply(lobby_for, interaction: discord.Interaction) -> "Lobby | None":
    """The press's lobby, or None (answered) if the server has no community."""
    lobby = lobby_for(interaction.guild)
    if lobby is None:
        await interaction.response.send_message("MonoBot's ladder isn't set up in this server.", ephemeral=True)
    return lobby


class LobbyView(discord.ui.View):
    """Join / Leave / New teams / End session, on the lobby message and on the
    current group's game summaries. Persistent and stateless: one registered
    instance serves every message in every server. The custom_ids predate the
    single line, so old messages keep working."""

    def __init__(self, lobby_for, teams: bool = True):
        super().__init__(timeout=None)
        self.lobby_for = lobby_for
        if not teams:
            # A queue with no game on has no teams to re-split or session to end.
            self.remove_item(self.new_teams)
            self.remove_item(self.end_session)

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

    @discord.ui.button(label="End session", style=discord.ButtonStyle.secondary, custom_id="monobot:match:end")
    async def end_session(self, interaction: discord.Interaction, button: discord.ui.Button):
        if lobby := await _lobby_or_reply(self.lobby_for, interaction):
            await lobby.handle_end_session(interaction)


class EndSessionView(discord.ui.View):
    """The private "are you sure?" behind End session: one tap from any of
    eight players shouldn't end a game the other seven are still playing."""

    def __init__(self, lobby: "Lobby"):
        super().__init__(timeout=60)
        self.lobby = lobby

    @discord.ui.button(label="End session", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.lobby.game_active():
            await interaction.response.edit_message(content="The session has already ended.", view=None)
            return
        await interaction.response.edit_message(content="Session ended — the queue's open.", view=None)
        await self.lobby.end_session(interaction.channel, interaction.guild, interaction.user.display_name)


class LegacyLineupView(discord.ui.View):
    """Pre-merge Sit out / Waitlist buttons still in chat: now Leave / Join."""

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
    """One community's line, live teams and sit-outs. `lobby_for` is passed to
    the views it posts so their buttons find it again."""

    def __init__(self, client, community: Community, lobby_for):
        self.client = client
        self.community = community
        self.config = community.config
        self.store = community.store
        self.ratings = community.ratings
        self.lobby_for = lobby_for
        # Whether a session was on at the last look, so the sweep can tell
        # when one runs out (notice_session_end).
        self.was_active = self.game_active()
        self.line: list[str] = self._stored_ids(LINE_META_KEY)
        self.sitting_out: list[str] = self._stored_ids(SITTING_OUT_META_KEY)
        # Users seen pressing a button or added by command, so forming a match
        # needn't look them up; anyone else is found in the server.
        self.members: dict[str, discord.abc.User] = {}
        # Last Join press per person in line, for the queue timeout.
        now = discord.utils.utcnow()
        self.joined_at: dict[str, dt.datetime] = {uid: now for uid in self.line}
        # The one live lobby message: the queue, or the teams while a game is on.
        self.message: discord.Message | None = None
        # Users, not QueuedPlayers, so a re-team reads fresh ratings.
        self.last_roster: list[discord.abc.User] = []
        # Ranked splits New teams cycles through until a game is stored.
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
        """(roster, promoted) with sit-outs swapped for the line, or why not."""
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

    async def notice_session_end(self):
        """Redraw as the queue once a session times out (no event fires for it)."""
        active = self.game_active()
        if self.was_active and not active:
            logger.info("Session ran out (community %s)", self.community.guild_id)
            await self._refresh()
        self.was_active = active

    async def end_session(self, channel: discord.abc.Messageable, guild: discord.Guild | None, by: str):
        """The line becomes the queue (clocks start now); players aren't
        re-queued, since most are leaving."""
        self.store.set_meta(MATCH_POSTED_AT_META_KEY, "")
        self.was_active = False
        self.options = []
        self.sitting_out.clear()
        for uid in self.line:
            self.renew(uid)
        self.save_lineup()
        logger.info("Session ended by %s (community %s)", by, self.community.guild_id)
        if len(self.line) >= QUEUE_TARGET and (roster := self._take_queue(guild)):
            await self.post_formed_match(channel, roster)
            return
        message = await channel.send(
            content=f"{by} ended the session — the queue's open.", embed=self._embed(), view=self._view()
        )
        await asyncio.gather(self._show(message), self._refresh(skip=message))

    # Wipe a queue left overnight, at queue_reset_time.
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

    # Drop players who wandered off, only while no game is on. Silent: a
    # reminder was too noisy, so the queue message states the rule.
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
        """Rated by their most-played account's standing (seed included), else
        their reported level."""
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
        """Redraw the lobby message and recent summaries' lineups, except `skip`."""
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
        """Make `message` the live lobby message, deleting the previous one."""
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
        """!queue: move the lobby to the bottom of chat. No role ping."""
        message = await ctx.send(embed=self._embed(), view=self._view())
        if ctx.interaction is not None:
            # A slash reply's token dies after 15 minutes; hold the fetched message.
            message = await ctx.channel.fetch_message(message.id)
        await self._show(message)

    # -- buttons ---------------------------------------------------------

    async def _answer(self, interaction: discord.Interaction, outcome: Outcome):
        """Answer a press within Discord's 3 seconds, before anything slow."""
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

    async def handle_end_session(self, interaction: discord.Interaction):
        """End session asks first, privately; EndSessionView does the ending.
        Only a player in the match may press it, as with New teams."""
        if not self.game_active():
            await interaction.response.send_message("No game's on — the lobby is already the queue.", ephemeral=True)
            return
        if str(interaction.user.id) not in self.stored_roster_ids():
            await interaction.response.send_message("Only a player in this match can end the session.", ephemeral=True)
            return
        await interaction.response.send_message(
            "End the session? The lobby goes back to the queue; anyone in line keeps their place.",
            view=EndSessionView(self),
            ephemeral=True,
        )

    async def handle_new_teams(self, interaction: discord.Interaction):
        """Players only. With no game stored and nobody sitting out, cycle the
        ranked splits; otherwise re-balance on current ratings, swapping
        sit-outs for the line."""
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
        """A member from a typed name (no partial matching), or None when the
        caller has been answered."""
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
        """Run `act(member, label)` for a typed name and post its Outcome."""

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
        """Record a level; refused once they've played."""
        uid = str(user.id)
        if self.has_played(uid):
            return "You've played here already, so your rating comes from your games now."
        self.store.set_skill_level(uid, level)
        return f"Got it — you'll start as {_level_label(level)}."

    # -- posting teams ---------------------------------------------------

    async def post_formed_match(self, channel: discord.abc.Messageable, users: list[discord.abc.User]):
        """Post a just-formed match. The roster is kept first, so if posting
        fails !teams can recover it."""
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
        """Balance on current ratings and post the teams as the new lobby
        message. `announce` pings everyone (new matches only); `promoted` are
        pinged regardless. Resets sit-outs."""
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
        # Post first, then tidy concurrently: serial round-trips looked frozen.
        cleanup = [self._show(message)]
        if lineup_changed:
            cleanup.append(self._refresh(skip=message))
        await asyncio.gather(*cleanup)

    def next_game_view(self, handles: set[str]) -> LobbyView | None:
        """Lobby buttons if at least half the live roster played this game."""
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
        self.timeout_sweep.start()

    async def cog_unload(self):
        for loop in self.daily_resets:
            loop.cancel()
        self.timeout_sweep.cancel()

    @tasks.loop(seconds=TIMEOUT_SWEEP_SECONDS)
    async def timeout_sweep(self):
        """Each community's queue timeouts, and whether its session has run
        out since the last look."""
        for community in self.client.communities.all():
            lobby = self.lobby_for(community)
            await lobby.notice_session_end()
            if community.config.queue_timeout_minutes:
                await lobby.sweep_timeouts()

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
