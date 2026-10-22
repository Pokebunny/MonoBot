"""Matchmaking queue: players join, and when the queue fills the bot splits
them into the two most balanced teams using their skill ratings.

Each community has its own Lobby — queue, waitlist and proposals — and the
cog routes every command and button press to the lobby of the server it came
from. The queue is an in-memory roster. discord.py runs interaction callbacks
on one event-loop thread, so no locking is needed.
"""

import asyncio
import collections
import datetime as dt
import logging

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

# meta key holding the live queue message pointer ("<channel_id>:<message_id>")
# so a message posted before a restart can still be found and cleaned up.
QUEUE_MSG_META_KEY = "queue_message"

# meta key holding the roster of the live proposal (comma-separated Discord
# ids), so New teams still works on a proposal posted before the last restart.
MATCH_ROSTER_META_KEY = "match_roster"

# meta keys holding who's sitting out the next game and who's waiting for a
# spot (comma-separated Discord ids, waitlist in join order). Stored so a
# deploy mid-session doesn't lose anyone's place in line.
SITTING_OUT_META_KEY = "match_sitting_out"
WAITLIST_META_KEY = "match_waitlist"

# meta key holding when the live proposal went up (ISO, UTC). Teams are
# re-posted for every game (New teams / !teams), so a recent post means a group
# is mid-session — the stored roster alone can't say, since it outlives the
# session it belonged to.
MATCH_POSTED_AT_META_KEY = "match_posted_at"

# How recently teams must have gone up for !add to treat a game as on: a long
# game plus the gap before someone re-teams.
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


class QueueView(discord.ui.View):
    """Join/Leave buttons attached to the queue message. Persistent (fixed
    custom_ids + registered via client.add_view on cog load), so the buttons
    keep working across bot restarts.

    Every view here takes `lobby_for(guild)` rather than a lobby: the one
    instance registered for persistence serves every server's messages, so
    each press finds its lobby from the server it came from."""

    def __init__(self, lobby_for):
        super().__init__(timeout=None)
        self.lobby_for = lobby_for

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="monobot:queue:join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if lobby := await _lobby_or_reply(self.lobby_for, interaction):
            await lobby.handle_join(interaction)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, custom_id="monobot:queue:leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if lobby := await _lobby_or_reply(self.lobby_for, interaction):
            await lobby.handle_leave(interaction)


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
    Join, or by !level. Not persistent — !level brings them back."""

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
                await interaction.response.send_message("Run `!level` to set your own.", ephemeral=True)
                return
            if lobby := await _lobby_or_reply(self.lobby_for, interaction):
                await interaction.response.edit_message(content=lobby.set_level(interaction.user, level), view=None)

        return pick


class ProposedMatchView(discord.ui.View):
    """The teams announced when the queue fills.

    One button, **New teams**, which does whichever of two things the moment
    calls for:

    - No games since this was posted: cycle the alternatives that were ranked
      at the time — same ratings, different split, edited in place.
    - A game has been played since: throw those away and re-split the roster
      from the players' *current* ratings, so the replay that just went up
      counts. Groups play a couple of games before re-teaming, and by then the
      ranked options no longer reflect where anyone stands.

    Asking which one they meant would be a worse button. The stale options are
    never what someone wants after a game, and before one there is nothing to
    re-balance from.

    The view is persistent (custom_id + registered via client.add_view on cog
    load) because deploys restart the bot under proposals that are still sitting
    in chat, and a click on one used to fail silently with nothing logged. What
    a restart does drop is the in-memory half — the ranked options and the index
    — so a restored view has nothing to cycle and goes straight to re-balancing,
    reading the roster back from MATCH_ROSTER_META_KEY.

    Only a player in the match may touch New teams.

    **Sit out** and **Waitlist** hand spots over between games. A player in
    the match presses Sit out to give up their spot for the next one; anyone
    else presses Waitlist to claim one. The swap happens at the next re-balance
    (New teams or `!teams`): everyone who didn't sit out keeps their spot, and
    each vacated one goes to the waitlist in the order people joined it. So a
    player who wants another game never has to race a newcomer to the Join
    button for it.
    """

    def __init__(
        self,
        lobby_for,
        users: list[discord.abc.User] | None = None,
        options: list[ProposedMatch] | None = None,
        games_at_post: int = 0,
    ):
        super().__init__(timeout=None)
        self.lobby_for = lobby_for
        self.users = list(users or [])  # kept so a re-balance can re-read live ratings
        self.options = list(options or [])
        self.index = 0
        # Games stored when this was posted; a higher count later means a
        # replay went up since, so the ranked options are stale.
        self.games_at_post = games_at_post

    def player_ids(self, lobby: "Lobby") -> set[str]:
        """Who may touch the button. Falls back to the stored roster for a
        restored view, whose users were lost with the restart."""
        if self.users:
            return {str(u.id) for u in self.users}
        return set(lobby.stored_roster_ids())

    def embed(self, lobby: "Lobby") -> discord.Embed:
        return lobby.lineup_embed(
            match_embeds.proposed_match(self.options[self.index], self.index, len(self.options), lobby.meeting_point())
        )

    async def _players_only(self, interaction: discord.Interaction, lobby: "Lobby", action: str) -> bool:
        if str(interaction.user.id) in self.player_ids(lobby):
            return True
        await interaction.response.send_message(f"Only a player in this match can {action} the teams.", ephemeral=True)
        return False

    @discord.ui.button(
        label="New teams", style=discord.ButtonStyle.primary, emoji="🔀", custom_id="monobot:match:newteams"
    )
    async def new_teams(self, interaction: discord.Interaction, button: discord.ui.Button):
        lobby = await _lobby_or_reply(self.lobby_for, interaction)
        if lobby is None or not await self._players_only(interaction, lobby, "re-team"):
            return
        if not self.options:
            # Restored view: the ranked splits went with the restart, so the
            # only thing left to offer is a fresh balance of the same roster.
            await self._rebalance(interaction, lobby, drop_message=True)
            return
        # Someone sitting out changes the roster, so cycling the old splits
        # would be wrong even if no game has been stored yet.
        if lobby.store.match_count() > self.games_at_post or lobby.sitting_out:
            await self._rebalance(interaction, lobby)
            return
        if len(self.options) <= 1:
            await interaction.response.send_message(
                "This roster only splits one sensible way, and no games have been played since.",
                ephemeral=True,
            )
            return
        self.index = (self.index + 1) % len(self.options)
        await interaction.response.edit_message(embed=self.embed(lobby), view=self)

    async def _rebalance(self, interaction: discord.Interaction, lobby: "Lobby", drop_message: bool = False):
        """Re-split from current ratings. Reposts at the bottom of the channel
        rather than editing in place: by now the old message has scrolled away
        under the games that were just played. post_match deletes the previous
        proposal, except a restored one it never had a handle on — hence
        drop_message."""
        users = self.users or lobby.resolve_roster(interaction.guild)
        if not users:
            await interaction.response.send_message(
                "This match is from before my last restart and I've lost its roster — run `!teams` to post fresh ones.",
                ephemeral=True,
            )
            return
        if await lobby.re_team(interaction, users) and drop_message:
            try:
                await interaction.message.delete()
            except discord.HTTPException:
                pass

    @discord.ui.button(
        label="Sit out", style=discord.ButtonStyle.secondary, emoji="🪑", custom_id="monobot:match:sitout"
    )
    async def sit_out(self, interaction: discord.Interaction, button: discord.ui.Button):
        if lobby := await _lobby_or_reply(self.lobby_for, interaction):
            await lobby.toggle_lineup(interaction, "sit_out", self.player_ids(lobby))

    @discord.ui.button(
        label="Waitlist", style=discord.ButtonStyle.secondary, emoji="⏳", custom_id="monobot:match:waitlist"
    )
    async def waitlist(self, interaction: discord.Interaction, button: discord.ui.Button):
        if lobby := await _lobby_or_reply(self.lobby_for, interaction):
            await lobby.toggle_lineup(interaction, "waitlist", self.player_ids(lobby))


class NextGameView(discord.ui.View):
    """Sit out / Waitlist / New teams on the summary of a game the current
    group just played. Once a game starts nobody scrolls back up to the teams
    message, but everyone sees the summary of the game they just finished, so
    that's where handing over spots for the next one has to live.

    Acts on the live roster (MATCH_ROSTER_META_KEY), not on the players in
    the summarised game. Persistent like the proposal, so a deploy doesn't
    kill the buttons on the summary everyone is looking at."""

    def __init__(self, lobby_for):
        super().__init__(timeout=None)
        self.lobby_for = lobby_for

    def track(self, message: discord.Message | None):
        """Record the message this view went out on, so lineup changes made
        elsewhere are redrawn on it too."""
        if message is not None and (lobby := self.lobby_for(getattr(message, "guild", None))):
            lobby.lineup_messages.append(message)

    @discord.ui.button(
        label="Sit out", style=discord.ButtonStyle.secondary, emoji="🪑", custom_id="monobot:next:sitout"
    )
    async def sit_out(self, interaction: discord.Interaction, button: discord.ui.Button):
        if lobby := await _lobby_or_reply(self.lobby_for, interaction):
            await lobby.toggle_lineup(interaction, "sit_out", set(lobby.stored_roster_ids()))

    @discord.ui.button(
        label="Waitlist", style=discord.ButtonStyle.secondary, emoji="⏳", custom_id="monobot:next:waitlist"
    )
    async def waitlist(self, interaction: discord.Interaction, button: discord.ui.Button):
        if lobby := await _lobby_or_reply(self.lobby_for, interaction):
            await lobby.toggle_lineup(interaction, "waitlist", set(lobby.stored_roster_ids()))

    @discord.ui.button(
        label="New teams", style=discord.ButtonStyle.primary, emoji="🔀", custom_id="monobot:next:newteams"
    )
    async def new_teams(self, interaction: discord.Interaction, button: discord.ui.Button):
        lobby = await _lobby_or_reply(self.lobby_for, interaction)
        if lobby is None:
            return
        if str(interaction.user.id) not in lobby.stored_roster_ids():
            await interaction.response.send_message("Only a player in this match can re-team.", ephemeral=True)
            return
        users = lobby.last_roster or lobby.resolve_roster(interaction.guild)
        if not users:
            await interaction.response.send_message(
                "I've lost the roster since my last restart — run `!teams` to post fresh teams.", ephemeral=True
            )
            return
        # The summary stays: it's the record of the game just played.
        await lobby.re_team(interaction, users)


class Lobby:
    """One community's matchmaking: its queue, the live proposal, and who's
    sitting out or waiting. `lobby_for` is what views it posts are given, so
    their buttons find this lobby again (after a restart too)."""

    def __init__(self, client, community: Community, lobby_for):
        self.client = client
        self.community = community
        self.config = community.config
        self.store = community.store
        self.ratings = community.ratings
        self.lobby_for = lobby_for
        self.queue: dict[str, discord.abc.User] = {}
        self.queue_message: discord.Message | None = None  # the live queue embed
        # The last roster to get teams, so !teams and New teams can re-split
        # it without anyone re-queuing. Users, not QueuedPlayers: ratings are
        # looked up fresh each time so re-teaming picks up recent games.
        self.last_roster: list[discord.abc.User] = []
        self.match_message: discord.Message | None = None  # the live proposal
        # Its view, so !waitlist can move the same split (and its ranked
        # alternatives) to the bottom of chat. Lost on restart.
        self.match_view: ProposedMatchView | None = None
        # The latest few messages carrying NextGameView (summaries, a !teams
        # still waiting on players), redrawn when the lineup changes.
        self.lineup_messages: collections.deque[discord.Message] = collections.deque(maxlen=3)
        # When each queued player last pressed Join; they're dropped once it's
        # queue_timeout_minutes old.
        self.joined_at: dict[str, dt.datetime] = {}
        # Spots changing hands at the next re-team (see ProposedMatchView).
        self.sitting_out: list[str] = self._stored_ids(SITTING_OUT_META_KEY)
        self.waitlist: list[str] = self._stored_ids(WAITLIST_META_KEY)

    # A queue that sat unfilled overnight is stale: people who joined, never
    # got a game and forgot to leave make the count look healthier than it is.
    # Wiped once a day in the small hours (the community's queue_reset_time).
    async def daily_reset(self):
        # Last night's waitlist is as stale as its queue.
        self.sitting_out.clear()
        self.waitlist.clear()
        self.save_lineup()
        if not self.queue:
            return
        stale = len(self.queue)
        self.queue.clear()
        logger.info("Daily queue reset (community %s): cleared %d player(s)", self.community.guild_id, stale)
        # Silent: the live queue message just edits back to empty, no new post.
        await self._refresh_message()

    # -- queue timeout -----------------------------------------------------

    # The daily reset catches a queue forgotten overnight; this catches one
    # player who wandered off mid-session and would otherwise be pinged into
    # a match they aren't there for. Silent on purpose: a reminder before
    # removal was tried and found too noisy, so the queue message states the
    # rule instead and pressing Join again restarts the clock.
    async def sweep_timeouts(self):
        self._forget_departed()
        timeout = dt.timedelta(minutes=self.config.queue_timeout_minutes)
        gone = expired(self.joined_at, discord.utils.utcnow(), timeout)
        for uid in gone:
            user = self.queue.pop(uid, None)
            del self.joined_at[uid]
            logger.info("Queue timeout: removed %s", user.display_name if user is not None else uid)
        if gone:
            await self._refresh_message()

    def renew(self, uid: str):
        """Start (or restart) a queued player's timeout."""
        self.joined_at[uid] = discord.utils.utcnow()

    def _forget_departed(self):
        """Drop timers for players no longer queued — they left, were bumped,
        or got a match. Done here rather than at every place the queue
        shrinks."""
        for uid in [uid for uid in self.joined_at if uid not in self.queue]:
            del self.joined_at[uid]

    async def _clear_old_queue_message(self, keep: discord.Message | None = None):
        """Delete the last-tracked queue message unless it's `keep`, then record
        `keep` as the current one. Called whenever a new queue message is posted
        or adopted, and on startup (keep=None), so exactly one live queue
        message survives and stale ones never accumulate — even across a restart,
        since the pointer lives in the DB, not just memory."""
        keep_ref = f"{keep.channel.id}:{keep.id}" if keep is not None else ""
        old_ref = self.store.get_meta(QUEUE_MSG_META_KEY) or ""
        if old_ref == keep_ref:
            return
        if old_ref:
            await self._delete_message_ref(old_ref)
        self.store.set_meta(QUEUE_MSG_META_KEY, keep_ref)

    async def _delete_message_ref(self, ref: str):
        """Delete a message given a stored "<channel_id>:<message_id>" pointer.
        Silent if it's already gone or the channel is unreachable."""
        try:
            channel_id, message_id = (int(part) for part in ref.split(":"))
        except ValueError:
            return
        channel = self.client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.client.fetch_channel(channel_id)
            except discord.HTTPException:
                return
        try:
            message = await channel.fetch_message(message_id)
            await message.delete()
        except discord.HTTPException:
            pass

    # -- rating lookup ---------------------------------------------------

    def _queued_player(self, user: discord.abc.User) -> QueuedPlayer:
        """Build a QueuedPlayer, rated by the user's bound SC2 account with the
        most games. A season's seed counts until they've played in it, so a
        soft reset balances on carried-over skill from the first game. Users
        who haven't played yet start from the level they reported (!level),
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

    def _players(self) -> list[QueuedPlayer]:
        return [self._queued_player(u) for u in self.queue.values()]

    def _status_embed(self) -> discord.Embed:
        return match_embeds.queue_status(
            self._players(), QUEUE_TARGET, self.config.queue_timeout_minutes, self.meeting_point()
        )

    def meeting_point(self) -> str | None:
        return match_embeds.meeting_point(self.config.sc2_chat_channel, self.config.sc2_region)

    async def _refresh_message(self):
        """Update the tracked queue message after a command changes the queue."""
        if self.queue_message is not None:
            try:
                await self.queue_message.edit(embed=self._status_embed(), view=QueueView(self.lobby_for))
            except discord.HTTPException:
                self.queue_message = None
                self.store.set_meta(QUEUE_MSG_META_KEY, "")

    async def _adopt_message(self, interaction: discord.Interaction):
        """Make the message the button lives on the one live queue message,
        deleting any previously tracked one so duplicates (leftover copies, or
        messages from before a restart) don't accumulate out of sync."""
        self.queue_message = interaction.message
        await self._clear_old_queue_message(keep=interaction.message)

    # -- commands & interactions -----------------------------------------

    async def open_queue(self, ctx):
        # Only one live queue message at a time: re-running !queue moves it to
        # the bottom of the chat rather than opening a duplicate. No role ping
        # — re-opening the queue mid-session is routine (re-teaming, a late
        # swap), and pinging the whole role each time is noise. Whoever wants
        # the community called in can @ the role themselves.
        self.queue_message = await ctx.send(
            embed=self._status_embed(),
            view=QueueView(self.lobby_for),
        )
        await self._clear_old_queue_message(keep=self.queue_message)

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

    async def bump(self, ctx, player: str):
        # Open to everyone: it only moves people in the queue and the next
        # game's lineup, which the next re-team or a Join press undoes.
        # Someone in the live match is sat out as if they'd pressed Sit out;
        # anyone else comes out of the queue or off the waitlist.
        async def picked(interaction, person):
            member = await self._member_of(ctx, person)
            await interaction.response.edit_message(content=await self._bump(member, person.sc2_name), view=None)

        member = await self._member_for(ctx, player, picked)
        if member is not None:
            await ctx.send(await self._bump(member, member.display_name))

    async def _bump(self, member, label: str) -> str:
        uid = str(member.id) if member is not None else None
        if uid is not None and uid in self.stored_roster_ids():
            return await self._bump_from_match(uid, label)
        queued = self.queue.pop(uid, None) is not None
        waiting = uid in self.waitlist
        if not (queued or waiting):
            return f"{label} isn't in the queue or on the waitlist."
        if queued:
            await self._refresh_message()
        if waiting:
            self.waitlist.remove(uid)
            self.save_lineup()
            await self._redraw_lineup()
        return f"Removed **{label}** from the {'queue' if queued else 'waitlist'}."

    async def _bump_from_match(self, uid: str, label: str) -> str:
        """Sit a player in the live match out of the next game, exactly as if
        they'd pressed Sit out: their spot goes to the first waitlister at the
        next re-team."""
        if uid in self.sitting_out:
            return f"**{label}** is already sitting out the next game."
        self.sitting_out.append(uid)
        self.save_lineup()
        await self._redraw_lineup()
        return f"**{label}** will sit out the next game; the first person on the waitlist takes their spot."

    async def teams(self, ctx):
        """Re-split the last match's roster without going back through the
        queue. Swapping people in and out is Sit out / Waitlist's job."""
        roster = list(self.last_roster)
        if not roster:
            await ctx.send("No recent match to re-team — run `!queue` to start one.")
            return
        lineup = self.lineup_for_next(ctx.guild, roster)
        if isinstance(lineup, str):
            # With the buttons, so whoever wants the spot can take it
            # right here and the group can re-team once they have.
            view = NextGameView(self.lobby_for)
            embed = self.lineup_embed(match_embeds.waiting_for_players(lineup))
            view.track(await ctx.send(embed=embed, view=view))
            return
        roster, promoted = lineup
        if len(roster) < 2 or len(roster) % 2 != 0:
            await ctx.send(f"Need an even number of players, got {len(roster)}.")
            return
        await self.post_match(ctx.channel, roster, promoted=promoted)

    async def show_lineup(self, ctx):
        """Bring the live teams, with who's sitting out and who's waiting, back
        to the bottom of chat without re-teaming — the Sit out / Waitlist
        buttons live on messages that scroll away once a game starts. Moves
        the proposal rather than copying it, so one set of teams stays live."""
        if self.match_view is not None and self.match_message is not None:
            message = await ctx.send(embed=self.match_view.embed(self), view=self.match_view)
            await self._clear_match_message(keep=message)
            return
        roster = self.stored_roster_ids()
        if not roster:
            await ctx.send("No match in progress — run `!queue` to start one.")
            return
        # A restart lost the split, but the roster and lineup are stored.
        view = NextGameView(self.lobby_for)
        view.track(await ctx.send(embed=self.lineup_embed(match_embeds.current_lineup(roster)), view=view))

    async def add(self, ctx, player: str):
        async def picked(interaction, person):
            member = await self._member_of(ctx, person)
            if member is None:
                await interaction.response.edit_message(
                    content=f"**{person.sc2_name}** isn't in this server.", view=None
                )
                return
            message, roster = await self._add_or_wait(member)
            await interaction.response.edit_message(content=message, view=None)
            await self._refresh_message()
            if roster:
                await self.post_formed_match(ctx.channel, roster)

        member = await self._member_for(ctx, player, picked)
        if member is None:
            return
        message, roster = await self._add_or_wait(member)
        await self._refresh_message()
        await ctx.send(message)
        if roster:
            await self.post_formed_match(ctx.channel, roster)

    async def _add_or_wait(self, member) -> tuple[str, list | None]:
        """!add's routing. With a game on and nobody queuing for another, a
        late arrival wants the next open spot, not a queue of one — so they go
        on the waitlist. Anyone already queuing means a second group is
        forming, and the add joins it."""
        if not self.queue and self.game_active() and str(member.id) not in self.stored_roster_ids():
            why = "A game's on, so "
            return await self.waitlist_member(member, member.display_name, why), None
        return self._add(member)

    def _add(self, member) -> tuple[str, list | None]:
        """Queue a member; returns what to say and a roster if that filled it."""
        uid = str(member.id)
        # Same link requirement as the Join button: an unlinked player would
        # queue on the new-player default and quietly skew the balance, so say
        # so rather than adding them.
        if not self.store.sc2_names_for(uid):
            return (
                f"**{member.display_name}** hasn't linked an SC2 name yet — "
                "they need to run `!link <their SC2 name>` before they can queue."
            ), None
        if uid in self.queue:
            self.renew(uid)
            return f"**{member.display_name}** is already in the queue — restarted their timer.", None
        self.queue[uid] = member
        self.renew(uid)
        roster = self._take_queue() if len(self.queue) >= QUEUE_TARGET else None
        return f"Added **{member.display_name}** to the queue.", roster

    async def clear_queue(self, ctx):
        self.queue.clear()
        await self._refresh_message()
        await ctx.send("Queue cleared.")

    async def handle_join(self, interaction: discord.Interaction):
        # Discord drops a press that isn't answered within 3 seconds, so
        # answer before anything slow: deleting an old queue message is a
        # round-trip, and the status embed can rebuild every rating. Run
        # first, those once lost the 8th join's press after the queue was
        # already emptied — no teams, and no queue to retry from.
        uid = str(interaction.user.id)
        if not self.store.sc2_names_for(uid):
            await interaction.response.send_message(
                "You need to link your SC2 name before you can queue. Run `!link <your SC2 name>` first.",
                ephemeral=True,
            )
            await self._adopt_message(interaction)
            return
        if uid in self.queue:
            self.renew(uid)
            await interaction.response.send_message(
                f"You're already in the queue — your timer's restarted, you'll stay in for another "
                f"{self.config.queue_timeout_minutes} minutes.",
                ephemeral=True,
            )
            await self._adopt_message(interaction)
            return
        await interaction.response.defer()
        self.queue[uid] = interaction.user
        self.renew(uid)
        roster = self._take_queue() if len(self.queue) >= QUEUE_TARGET else None
        await self._adopt_message(interaction)
        # Reset the queue message either way, then announce any formed match.
        await interaction.edit_original_response(embed=self._status_embed(), view=QueueView(self.lobby_for))
        if self.needs_level(uid):
            view = SkillLevelView(self.lobby_for, interaction.user.id)
            await interaction.followup.send(LEVEL_PROMPT, view=view, ephemeral=True)
        if roster:
            await self.post_formed_match(interaction.channel, roster)

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
        return (
            f"Got it — you'll start as {_level_label(level)}. Change it with `!level` any time before your first game."
        )

    async def level_prompt(self, ctx):
        uid = str(ctx.author.id)
        if self.has_played(uid):
            await ctx.send("You've played here already, so your rating comes from your games now.", ephemeral=True)
            return
        current = self.store.skill_level(uid)
        content = LEVEL_PROMPT if current is None else f"You're down as {_level_label(current)}. Change it?"
        await ctx.send(content, view=SkillLevelView(self.lobby_for, ctx.author.id), ephemeral=True)

    async def handle_leave(self, interaction: discord.Interaction):
        # Answered first, like handle_join, so the press can't time out.
        uid = str(interaction.user.id)
        if self.queue.pop(uid, None) is None:
            await interaction.response.send_message("You're not in the queue.", ephemeral=True)
            await self._adopt_message(interaction)
            return
        await interaction.response.defer()
        await self._adopt_message(interaction)
        await interaction.edit_original_response(embed=self._status_embed(), view=QueueView(self.lobby_for))

    def _take_queue(self) -> list[discord.abc.User]:
        """Empty the queue and hand back the roster for a match. Callers refresh
        the queue message themselves — through the interaction for a button
        join, through _refresh_message for an admin !add."""
        users = list(self.queue.values())[:QUEUE_TARGET]
        self.queue.clear()
        return users

    async def post_formed_match(self, channel: discord.abc.Messageable, users: list[discord.abc.User]):
        """Announce the match a full queue just formed. The queue is already
        emptied, so a failure here would otherwise drop the eight players on
        the floor: the roster is kept first, and if posting fails anyway the
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
        """Balance `users` from their current ratings and post the proposal at
        the bottom of the channel, superseding any previous one.

        The ratings are read here rather than passed in, so every route into
        this (queue filling, New teams, !teams) reflects games played since
        the last split. `announce` mentions the players — on for a freshly
        formed match, off for a re-team, where everyone is already watching
        and eight pings per re-roll would be spam. `promoted` are waitlisters
        who just took a spot; they're pinged even on a re-team, since they
        aren't watching the way the players are.

        Starts a fresh lineup: sit-outs belonged to the old roster, and anyone
        now playing comes off the waitlist and out of the queue."""
        players = [self._queued_player(u) for u in users]
        options = ranked_matches(players, limit=SHUFFLE_OPTIONS)
        self.last_roster = list(users)
        self.store.set_meta(MATCH_ROSTER_META_KEY, ",".join(str(u.id) for u in users))
        self.store.set_meta(MATCH_POSTED_AT_META_KEY, discord.utils.utcnow().isoformat())
        roster_ids = {str(u.id) for u in users}
        waiting = len(self.waitlist)
        lineup_changed = bool(self.sitting_out)
        self.sitting_out.clear()
        self.waitlist[:] = [uid for uid in self.waitlist if uid not in roster_ids]
        lineup_changed |= len(self.waitlist) != waiting
        self.save_lineup()
        queue_changed = any([self.queue.pop(str(u.id), None) for u in promoted])
        view = ProposedMatchView(self.lobby_for, list(users), options, self.store.match_count())
        pinged = list(users) if announce else list(promoted)
        content = None
        if announce:
            content = " ".join(f"<@{u.id}>" for u in users) + " — your match is ready!"
        elif promoted:
            content = " ".join(f"<@{u.id}>" for u in promoted) + " — a spot opened up, you're in!"
        message = await channel.send(
            content=content,
            embed=view.embed(self),
            view=view,
            allowed_mentions=discord.AllowedMentions(users=pinged),
        )
        self.match_view = view
        # Teams go up first and the tidying after, all at once: each of these
        # is a Discord round-trip, and run one by one ahead of the send they
        # made a re-team look frozen for seconds. The old proposal is deleted
        # rather than redrawn, so only the summaries need the cleared lineup.
        cleanup = [self._clear_match_message(keep=message)]
        if lineup_changed:
            cleanup.append(self._redraw_lineup(skip=message))
        if queue_changed:
            cleanup.append(self._refresh_message())
        await asyncio.gather(*cleanup)

    # -- sit-outs & waitlist ----------------------------------------------

    def _stored_ids(self, key: str) -> list[str]:
        return [i for i in (self.store.get_meta(key) or "").split(",") if i]

    def save_lineup(self):
        self.store.set_meta(SITTING_OUT_META_KEY, ",".join(self.sitting_out))
        self.store.set_meta(WAITLIST_META_KEY, ",".join(self.waitlist))

    def lineup_embed(self, embed: discord.Embed) -> discord.Embed:
        return match_embeds.with_lineup_changes(embed, self.sitting_out, self.waitlist)

    async def _redraw_lineup(self, skip: discord.Message | None = None):
        """Redraw the lineup fields everywhere they're shown — the live
        proposal and the latest summary — except `skip`, which the caller
        has just redrawn itself. Reads each embed back off its message, so a
        summary keeps its own fields."""
        for message in (self.match_message, *self.lineup_messages):
            if message is None or not message.embeds or (skip is not None and message.id == skip.id):
                continue
            try:
                await message.edit(embed=self.lineup_embed(message.embeds[0]))
            except discord.HTTPException:
                pass

    async def toggle_lineup(self, interaction: discord.Interaction, action: str, roster_ids: set[str]):
        """A Sit out ("sit_out") or Waitlist ("waitlist") press. Players in
        `roster_ids` may sit out, anyone else may wait; pressing again undoes
        either. Replies privately and redraws the lineup where it's shown."""
        uid = str(interaction.user.id)
        playing = uid in roster_ids
        if action == "sit_out" and not playing:
            reply = "Only a player in this match can sit out — press **Waitlist** to wait for a spot."
            await interaction.response.send_message(reply, ephemeral=True)
            return
        if action == "waitlist" and playing:
            reply = "You're already in this match — press **Sit out** to give up your spot."
            await interaction.response.send_message(reply, ephemeral=True)
            return
        if action == "sit_out" and uid in self.sitting_out:
            self.sitting_out.remove(uid)
            reply = "You're back in for the next game."
        elif action == "sit_out":
            self.sitting_out.append(uid)
            reply = (
                "You'll sit out the next game; the first person on the waitlist takes your spot. "
                "Press again to change your mind."
            )
        elif uid in self.waitlist:
            self.waitlist.remove(uid)
            reply = "You've left the waitlist."
        elif not self.store.sc2_names_for(uid):
            reply = "You need to link your SC2 name before you can play. Run `!link <your SC2 name>` first."
            await interaction.response.send_message(reply, ephemeral=True)
            return
        else:
            self.waitlist.append(uid)
            reply = (
                f"You're #{len(self.waitlist)} on the waitlist — you'll be pinged when a spot opens. "
                "Press again to leave."
            )
        self.save_lineup()
        await interaction.response.send_message(reply, ephemeral=True)
        try:
            await interaction.message.edit(embed=self.lineup_embed(interaction.message.embeds[0]))
        except discord.HTTPException, IndexError:
            pass
        await self._redraw_lineup(skip=interaction.message)

    async def add_to_waitlist(self, ctx, player: str):
        """!waitlist <name>: put someone on the waitlist for them, as if
        they'd pressed Waitlist."""

        async def picked(interaction, person):
            member = await self._member_of(ctx, person)
            if member is None:
                await interaction.response.edit_message(
                    content=f"**{person.sc2_name}** isn't in this server.", view=None
                )
                return
            await interaction.response.edit_message(
                content=await self.waitlist_member(member, person.sc2_name), view=None
            )

        member = await self._member_for(ctx, player, picked)
        if member is not None:
            await ctx.send(await self.waitlist_member(member, member.display_name))

    async def waitlist_member(self, member, label: str, why: str = "") -> str:
        """Append a member to the waitlist and redraw it; says what happened.
        `why` prefixes the success message."""
        uid = str(member.id)
        if uid in self.stored_roster_ids():
            return f"**{label}** is already in the match — `!bump` sits them out of the next game."
        if uid in self.waitlist:
            return f"**{label}** is already #{self.waitlist.index(uid) + 1} on the waitlist."
        if not self.store.sc2_names_for(uid):
            return (
                f"**{label}** hasn't linked an SC2 name yet — "
                "they need to run `!link <their SC2 name>` before they can play."
            )
        self.waitlist.append(uid)
        self.save_lineup()
        await self._redraw_lineup()
        return f"{why}**{label}** is #{len(self.waitlist)} on the waitlist — they'll take the next open spot."

    def game_active(self) -> bool:
        """Whether a group is mid-session: teams went up within the last
        ACTIVE_GAME_MINUTES."""
        posted = self.store.get_meta(MATCH_POSTED_AT_META_KEY)
        if not posted or not self.stored_roster_ids():
            return False
        age = discord.utils.utcnow() - dt.datetime.fromisoformat(posted)
        return age < dt.timedelta(minutes=ACTIVE_GAME_MINUTES)

    async def re_team(self, interaction: discord.Interaction, users: list[discord.abc.User]) -> bool:
        """Swap sit-outs for waitlisters and post fresh teams; answers the
        interaction either way. False if the swap couldn't happen yet."""
        lineup = self.lineup_for_next(interaction.guild, users)
        if isinstance(lineup, str):
            await interaction.response.send_message(lineup, ephemeral=True)
            return False
        users, promoted = lineup
        await interaction.response.defer()
        await self.post_match(interaction.channel, users, promoted=promoted)
        return True

    def next_game_view(self, handles: set[str]) -> NextGameView | None:
        """Lineup buttons for the summary of a game with these players'
        toon handles, if it was the current group's game — at least half the
        live roster played in it. None for anyone else's game, where handing
        over spots in this roster would make no sense."""
        roster = self.stored_roster_ids()
        if not roster:
            return None
        played = sum(1 for uid in roster if handles & set(self.store.handles_for(uid)))
        return NextGameView(self.lobby_for) if played * 2 >= len(roster) else None

    def lineup_for_next(
        self, guild: discord.Guild | None, users: list[discord.abc.User]
    ) -> tuple[list[discord.abc.User], list[discord.abc.User]] | str:
        """`users` with sit-outs swapped for waitlisters: (roster, promoted),
        or a message saying why the swap can't happen yet. Waitlisters who've
        left the server are skipped, not counted."""
        members = {str(u.id): u for u in users}
        if guild is not None:
            for uid in self.waitlist:
                member = guild.get_member(int(uid))
                if member is not None:
                    members.setdefault(uid, member)
        waiting = [uid for uid in self.waitlist if uid in members]
        roster, promoted, short = next_roster([str(u.id) for u in users], self.sitting_out, waiting)
        if short:
            return (
                f"{len(self.sitting_out)} sitting out but only {len(promoted)} on the waitlist — "
                f"need {short} more. Press **Waitlist** to take a spot, or press **Sit out** again to stay in."
            )
        return [members[uid] for uid in roster], [members[uid] for uid in promoted]

    def stored_roster_ids(self) -> list[str]:
        """Discord ids of the live proposal's roster, as last posted."""
        return [i for i in (self.store.get_meta(MATCH_ROSTER_META_KEY) or "").split(",") if i]

    def resolve_roster(self, guild: discord.Guild | None) -> list[discord.abc.User]:
        """The stored roster as members of `guild`. Empty if it can't be fully
        resolved — re-teaming a partial roster would silently drop players."""
        if guild is None:
            return []
        members = [guild.get_member(int(i)) for i in self.stored_roster_ids()]
        return [m for m in members if m is not None] if all(m is not None for m in members) else []

    async def _clear_match_message(self, keep: discord.Message | None = None):
        """Delete the previous proposal so exactly one set of teams is live and
        players can't act on a superseded split. In-memory only, unlike the
        queue pointer: a proposal is fleeting and needn't survive a restart."""
        old = self.match_message
        self.match_message = keep
        if old is None or (keep is not None and old.id == keep.id):
            return
        try:
            await old.delete()
        except discord.HTTPException:
            pass


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
        # Register the persistent views so Join/Leave on queue messages, New
        # teams on proposals and the lineup buttons on match summaries from
        # before the last restart still dispatch here.
        self.client.add_view(QueueView(self.lobby_for))
        self.client.add_view(ProposedMatchView(self.lobby_for))
        self.client.add_view(NextGameView(self.lobby_for))
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
        # Sweep a queue message left over from before a restart so a stale,
        # unbacked queue isn't left sitting in chat. keep=lobby.queue_message is
        # None on a fresh process (deletes the leftover) but the live message
        # on a mid-session gateway reconnect (so it's preserved, not deleted).
        for community in self.client.communities.all():
            lobby = self.lobby_for(community)
            await lobby._clear_old_queue_message(keep=lobby.queue_message)

    @commands.hybrid_command(help="open the matchmaking queue")
    @commands.cooldown(1, 30, commands.BucketType.channel)
    async def queue(self, ctx):
        await self.lobby_for(ctx.guild).open_queue(ctx)

    @commands.hybrid_command(
        aliases=["remove"], help="sit a player out of the next game, or take them out of the queue"
    )
    async def bump(self, ctx, *, player: str):
        await self.lobby_for(ctx.guild).bump(ctx, player)

    @commands.hybrid_command(help="re-post the last match with freshly balanced teams")
    @commands.cooldown(1, 10, commands.BucketType.channel)
    async def teams(self, ctx):
        await self.lobby_for(ctx.guild).teams(ctx)

    @commands.hybrid_command(
        name="waitlist",
        aliases=["lineup"],
        help="show the current teams and waitlist at the bottom of chat, or put a player on the waitlist",
    )
    @commands.cooldown(1, 10, commands.BucketType.channel)
    async def show_lineup(self, ctx, *, player: str | None = None):
        if player:
            # The cooldown is there to stop the lineup being re-posted over and
            # over; adding a few people in a row is fine.
            ctx.command.reset_cooldown(ctx)
            await self.lobby_for(ctx.guild).add_to_waitlist(ctx, player)
            return
        await self.lobby_for(ctx.guild).show_lineup(ctx)

    @commands.hybrid_command(help="put a player into the queue (or on the waitlist while a game's on)")
    async def add(self, ctx, *, player: str):
        await self.lobby_for(ctx.guild).add(ctx, player)

    @commands.hybrid_command(help="set your SC2 level, which seeds your rating before your first game")
    async def level(self, ctx):
        await self.lobby_for(ctx.guild).level_prompt(ctx)

    @commands.hybrid_command(help="clear the matchmaking queue")
    async def clearqueue(self, ctx):
        await self.lobby_for(ctx.guild).clear_queue(ctx)


async def setup(client):
    await client.add_cog(Matchmaking(client))
