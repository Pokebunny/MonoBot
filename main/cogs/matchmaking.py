"""Matchmaking queue: players join, and when the queue fills the bot splits
them into the two most balanced teams using their skill ratings.

The queue is a single in-memory roster (one queue for the bot). discord.py
runs interaction callbacks on one event-loop thread, so no locking is needed.
"""

import collections
import datetime as dt
import logging
import zoneinfo

import discord
from checks import is_bot_admin
from discord.ext import commands, tasks
from models.matchmaking import ProposedMatch, QueuedPlayer
from resources.config import CONFIG
from services import match_embeds
from services.matchmaking import expired, next_roster, ranked_matches
from services.rating import DEFAULT_MU, DEFAULT_SIGMA, RatingCache
from services.storage import MatchStore
from views import person_or_pick

logger = logging.getLogger(__name__)

QUEUE_TARGET = 8  # 4v4

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

# How often queue timeouts are checked; the slack on the timeout itself.
TIMEOUT_SWEEP_SECONDS = 60


def _reset_time() -> dt.time | None:
    """The configured daily queue-reset time, or None if it's disabled or
    unparseable (a bad value shouldn't stop the bot from booting)."""
    raw = CONFIG.queue_reset_time
    if not raw:
        return None
    try:
        tz = zoneinfo.ZoneInfo(CONFIG.queue_reset_timezone)
        hour, minute = (int(part) for part in raw.split(":"))
        return dt.time(hour=hour, minute=minute, tzinfo=tz)
    except ValueError, zoneinfo.ZoneInfoNotFoundError:
        logger.warning(
            "Ignoring bad queue reset config (%r in %r); daily reset disabled",
            raw,
            CONFIG.queue_reset_timezone,
        )
        return None


class QueueView(discord.ui.View):
    """Join/Leave buttons attached to the queue message. Persistent (fixed
    custom_ids + registered via client.add_view on cog load), so the buttons
    keep working across bot restarts."""

    def __init__(self, cog: "Matchmaking"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="monobot:queue:join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_join(interaction)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, custom_id="monobot:queue:leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_leave(interaction)


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
        cog: "Matchmaking",
        users: list[discord.abc.User] | None = None,
        options: list[ProposedMatch] | None = None,
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.users = list(users or [])  # kept so a re-balance can re-read live ratings
        self.options = list(options or [])
        self.index = 0
        # Games stored when this was posted; a higher count later means a
        # replay went up since, so the ranked options are stale.
        self.games_at_post = cog.store.match_count()

    @property
    def player_ids(self) -> set[str]:
        """Who may touch the button. Falls back to the stored roster for a
        restored view, whose users were lost with the restart."""
        if self.users:
            return {str(u.id) for u in self.users}
        return set(self.cog.stored_roster_ids())

    def embed(self) -> discord.Embed:
        return self.cog.lineup_embed(
            match_embeds.proposed_match(self.options[self.index], self.index, len(self.options))
        )

    async def _players_only(self, interaction: discord.Interaction, action: str) -> bool:
        if str(interaction.user.id) in self.player_ids:
            return True
        await interaction.response.send_message(f"Only a player in this match can {action} the teams.", ephemeral=True)
        return False

    @discord.ui.button(
        label="New teams", style=discord.ButtonStyle.primary, emoji="🔀", custom_id="monobot:match:newteams"
    )
    async def new_teams(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._players_only(interaction, "re-team"):
            return
        if not self.options:
            # Restored view: the ranked splits went with the restart, so the
            # only thing left to offer is a fresh balance of the same roster.
            await self._rebalance(interaction, drop_message=True)
            return
        # Someone sitting out changes the roster, so cycling the old splits
        # would be wrong even if no game has been stored yet.
        if self.cog.store.match_count() > self.games_at_post or self.cog.sitting_out:
            await self._rebalance(interaction)
            return
        if len(self.options) <= 1:
            await interaction.response.send_message(
                "This roster only splits one sensible way, and no games have been played since.",
                ephemeral=True,
            )
            return
        self.index = (self.index + 1) % len(self.options)
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _rebalance(self, interaction: discord.Interaction, drop_message: bool = False):
        """Re-split from current ratings. Reposts at the bottom of the channel
        rather than editing in place: by now the old message has scrolled away
        under the games that were just played. post_match deletes the previous
        proposal, except a restored one it never had a handle on — hence
        drop_message."""
        users = self.users or self.cog.resolve_roster(interaction.guild)
        if not users:
            await interaction.response.send_message(
                "This match is from before my last restart and I've lost its roster — run `!teams` to post fresh ones.",
                ephemeral=True,
            )
            return
        if await self.cog.re_team(interaction, users) and drop_message:
            try:
                await interaction.message.delete()
            except discord.HTTPException:
                pass

    @discord.ui.button(
        label="Sit out", style=discord.ButtonStyle.secondary, emoji="🪑", custom_id="monobot:match:sitout"
    )
    async def sit_out(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.toggle_lineup(interaction, "sit_out", self.player_ids)

    @discord.ui.button(
        label="Waitlist", style=discord.ButtonStyle.secondary, emoji="⏳", custom_id="monobot:match:waitlist"
    )
    async def waitlist(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.toggle_lineup(interaction, "waitlist", self.player_ids)


class NextGameView(discord.ui.View):
    """Sit out / Waitlist / New teams on the summary of a game the current
    group just played. Once a game starts nobody scrolls back up to the teams
    message, but everyone sees the summary of the game they just finished, so
    that's where handing over spots for the next one has to live.

    Acts on the live roster (MATCH_ROSTER_META_KEY), not on the players in
    the summarised game. Persistent like the proposal, so a deploy doesn't
    kill the buttons on the summary everyone is looking at."""

    def __init__(self, cog: "Matchmaking"):
        super().__init__(timeout=None)
        self.cog = cog

    def track(self, message: discord.Message | None):
        """Record the message this view went out on, so lineup changes made
        elsewhere are redrawn on it too."""
        if message is not None:
            self.cog.lineup_messages.append(message)

    @discord.ui.button(
        label="Sit out", style=discord.ButtonStyle.secondary, emoji="🪑", custom_id="monobot:next:sitout"
    )
    async def sit_out(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.toggle_lineup(interaction, "sit_out", set(self.cog.stored_roster_ids()))

    @discord.ui.button(
        label="Waitlist", style=discord.ButtonStyle.secondary, emoji="⏳", custom_id="monobot:next:waitlist"
    )
    async def waitlist(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.toggle_lineup(interaction, "waitlist", set(self.cog.stored_roster_ids()))

    @discord.ui.button(
        label="New teams", style=discord.ButtonStyle.primary, emoji="🔀", custom_id="monobot:next:newteams"
    )
    async def new_teams(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) not in self.cog.stored_roster_ids():
            await interaction.response.send_message("Only a player in this match can re-team.", ephemeral=True)
            return
        users = self.cog.last_roster or self.cog.resolve_roster(interaction.guild)
        if not users:
            await interaction.response.send_message(
                "I've lost the roster since my last restart — run `!teams` to post fresh teams.", ephemeral=True
            )
            return
        # The summary stays: it's the record of the game just played.
        await self.cog.re_team(interaction, users)


class Matchmaking(commands.Cog):
    def __init__(self, client):
        self.client = client
        if not hasattr(client, "match_store"):
            client.match_store = MatchStore()
        if not hasattr(client, "rating_cache"):
            client.rating_cache = RatingCache(client.match_store)
        self.store: MatchStore = client.match_store
        self.ratings: RatingCache = client.rating_cache
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
        # CONFIG.queue_timeout_minutes old.
        self.joined_at: dict[str, dt.datetime] = {}
        # Spots changing hands at the next re-team (see ProposedMatchView).
        self.sitting_out: list[str] = self._stored_ids(SITTING_OUT_META_KEY)
        self.waitlist: list[str] = self._stored_ids(WAITLIST_META_KEY)

    async def cog_load(self):
        # Register the persistent view so Join/Leave buttons on queue messages
        # from before the last restart still dispatch here.
        self.client.add_view(QueueView(self))
        # Same for New teams on a proposal that outlived the restart, and the
        # lineup buttons on match summaries.
        self.client.add_view(ProposedMatchView(self))
        self.client.add_view(NextGameView(self))
        reset_at = _reset_time()
        if reset_at is not None:
            self.daily_reset.change_interval(time=reset_at)
            self.daily_reset.start()
        if CONFIG.queue_timeout_minutes:
            self.timeout_sweep.start()

    async def cog_unload(self):
        self.daily_reset.cancel()
        self.timeout_sweep.cancel()

    # A queue that sat unfilled overnight is stale: people who joined, never
    # got a game and forgot to leave make the count look healthier than it is.
    # Wipe it once a day in the small hours (real time set in cog_load).
    @tasks.loop(time=dt.time(hour=5))
    async def daily_reset(self):
        # Last night's waitlist is as stale as its queue.
        self.sitting_out.clear()
        self.waitlist.clear()
        self.save_lineup()
        if not self.queue:
            return
        stale = len(self.queue)
        self.queue.clear()
        logger.info("Daily queue reset: cleared %d player(s)", stale)
        # Silent: the live queue message just edits back to empty, no new post.
        await self._refresh_message()

    @daily_reset.before_loop
    async def before_daily_reset(self):
        await self.client.wait_until_ready()

    # -- queue timeout -----------------------------------------------------

    # The daily reset catches a queue forgotten overnight; this catches one
    # player who wandered off mid-session and would otherwise be pinged into
    # a match they aren't there for. Silent on purpose: a reminder before
    # removal was tried and found too noisy, so the queue message states the
    # rule instead and pressing Join again restarts the clock.
    @tasks.loop(seconds=TIMEOUT_SWEEP_SECONDS)
    async def timeout_sweep(self):
        self._forget_departed()
        gone = expired(self.joined_at, discord.utils.utcnow(), dt.timedelta(minutes=CONFIG.queue_timeout_minutes))
        for uid in gone:
            user = self.queue.pop(uid, None)
            del self.joined_at[uid]
            logger.info("Queue timeout: removed %s", user.display_name if user is not None else uid)
        if gone:
            await self._refresh_message()

    @timeout_sweep.before_loop
    async def before_timeout_sweep(self):
        await self.client.wait_until_ready()

    @timeout_sweep.error
    async def timeout_sweep_error(self, error):
        # A loop task dies on an unhandled exception; log it and keep sweeping.
        logger.exception("Queue timeout sweep failed", exc_info=error)
        self.timeout_sweep.restart()

    def renew(self, uid: str):
        """Start (or restart) a queued player's timeout."""
        self.joined_at[uid] = discord.utils.utcnow()

    def _forget_departed(self):
        """Drop timers for players no longer queued — they left, were bumped,
        or got a match. Done here rather than at every place the queue
        shrinks."""
        for uid in [uid for uid in self.joined_at if uid not in self.queue]:
            del self.joined_at[uid]

    @commands.Cog.listener()
    async def on_ready(self):
        # Sweep a queue message left over from before a restart so a stale,
        # unbacked queue isn't left sitting in chat. keep=self.queue_message is
        # None on a fresh process (deletes the leftover) but the live message
        # on a mid-session gateway reconnect (so it's preserved, not deleted).
        await self._clear_old_queue_message(keep=self.queue_message)

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
        most games. Users who are linked but haven't played yet (no bound
        handle) get the new-player default rating."""
        book = self.ratings.book()
        best = None
        for handle in self.store.handles_for(str(user.id)):
            rating = book.rating_for(handle)  # follows account merges
            if rating is not None and (best is None or rating.games > best.games):
                best = rating
        if best is not None:
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
            mu=DEFAULT_MU,
            sigma=DEFAULT_SIGMA,
        )

    def _players(self) -> list[QueuedPlayer]:
        return [self._queued_player(u) for u in self.queue.values()]

    def _status_embed(self) -> discord.Embed:
        return match_embeds.queue_status(self._players(), QUEUE_TARGET, CONFIG.queue_timeout_minutes)

    async def _refresh_message(self):
        """Update the tracked queue message after a command changes the queue."""
        if self.queue_message is not None:
            try:
                await self.queue_message.edit(embed=self._status_embed(), view=QueueView(self))
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

    @commands.hybrid_command(help="open the matchmaking queue")
    @commands.cooldown(1, 30, commands.BucketType.channel)
    async def queue(self, ctx):
        # Only one live queue message at a time: re-running !queue moves it to
        # the bottom of the chat rather than opening a duplicate. No role ping
        # — re-opening the queue mid-session is routine (re-teaming, a late
        # swap), and pinging the whole role each time is noise. Whoever wants
        # the community called in can @ the role themselves.
        self.queue_message = await ctx.send(
            embed=self._status_embed(),
            view=QueueView(self),
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

    @commands.hybrid_command(aliases=["remove"], help="remove a player from the queue, e.g. a no-show (mods)")
    @is_bot_admin()
    async def bump(self, ctx, *, player: str):
        # Admin-gated: players drop themselves with Leave, so this exists only
        # to clear someone else out, which shouldn't be open to everyone.
        async def picked(interaction, person):
            member = await self._member_of(ctx, person)
            await interaction.response.edit_message(content=await self._bump(member, person.sc2_name), view=None)

        member = await self._member_for(ctx, player, picked)
        if member is not None:
            await ctx.send(await self._bump(member, member.display_name))

    async def _bump(self, member, label: str) -> str:
        uid = str(member.id) if member is not None else None
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

    @commands.hybrid_command(help="re-post the last match with freshly balanced teams")
    @commands.cooldown(1, 10, commands.BucketType.channel)
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
            view = NextGameView(self)
            embed = self.lineup_embed(match_embeds.waiting_for_players(lineup))
            view.track(await ctx.send(embed=embed, view=view))
            return
        roster, promoted = lineup
        if len(roster) < 2 or len(roster) % 2 != 0:
            await ctx.send(f"Need an even number of players, got {len(roster)}.")
            return
        await self.post_match(ctx.channel, roster, promoted=promoted)

    @commands.hybrid_command(
        name="waitlist", aliases=["lineup"], help="show the current teams and waitlist at the bottom of chat"
    )
    @commands.cooldown(1, 10, commands.BucketType.channel)
    async def show_lineup(self, ctx):
        """Bring the live teams, with who's sitting out and who's waiting, back
        to the bottom of chat without re-teaming — the Sit out / Waitlist
        buttons live on messages that scroll away once a game starts. Moves
        the proposal rather than copying it, so one set of teams stays live."""
        if self.match_view is not None and self.match_message is not None:
            message = await ctx.send(embed=self.match_view.embed(), view=self.match_view)
            await self._clear_match_message(keep=message)
            return
        roster = self.stored_roster_ids()
        if not roster:
            await ctx.send("No match in progress — run `!queue` to start one.")
            return
        # A restart lost the split, but the roster and lineup are stored.
        view = NextGameView(self)
        view.track(await ctx.send(embed=self.lineup_embed(match_embeds.current_lineup(roster)), view=view))

    @commands.hybrid_command(help="put a player into the queue (mods)")
    @is_bot_admin()
    async def add(self, ctx, *, player: str):
        async def picked(interaction, person):
            member = await self._member_of(ctx, person)
            if member is None:
                await interaction.response.edit_message(
                    content=f"**{person.sc2_name}** isn't in this server.", view=None
                )
                return
            message, roster = self._add(member)
            await interaction.response.edit_message(content=message, view=None)
            await self._refresh_message()
            if roster:
                await self.post_match(ctx.channel, roster, announce=True)

        member = await self._member_for(ctx, player, picked)
        if member is None:
            return
        message, roster = self._add(member)
        await self._refresh_message()
        await ctx.send(message)
        if roster:
            await self.post_match(ctx.channel, roster, announce=True)

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

    @commands.hybrid_command(help="clear the matchmaking queue (mods)")
    @is_bot_admin()
    async def clearqueue(self, ctx):
        self.queue.clear()
        await self._refresh_message()
        await ctx.send("Queue cleared.")

    async def handle_join(self, interaction: discord.Interaction):
        await self._adopt_message(interaction)
        uid = str(interaction.user.id)
        if not self.store.sc2_names_for(uid):
            await interaction.response.send_message(
                "You need to link your SC2 name before you can queue. Run `!link <your SC2 name>` first.",
                ephemeral=True,
            )
            return
        if uid in self.queue:
            self.renew(uid)
            await interaction.response.send_message(
                f"You're already in the queue — your timer's restarted, you'll stay in for another "
                f"{CONFIG.queue_timeout_minutes} minutes.",
                ephemeral=True,
            )
            return
        self.queue[uid] = interaction.user
        self.renew(uid)
        roster = self._take_queue() if len(self.queue) >= QUEUE_TARGET else None
        # Reset the queue message either way, then announce any formed match.
        await interaction.response.edit_message(embed=self._status_embed(), view=QueueView(self))
        if roster:
            await self.post_match(interaction.channel, roster, announce=True)

    async def handle_leave(self, interaction: discord.Interaction):
        await self._adopt_message(interaction)
        uid = str(interaction.user.id)
        if self.queue.pop(uid, None) is None:
            await interaction.response.send_message("You're not in the queue.", ephemeral=True)
            return
        await interaction.response.edit_message(embed=self._status_embed(), view=QueueView(self))

    def _take_queue(self) -> list[discord.abc.User]:
        """Empty the queue and hand back the roster for a match. Callers refresh
        the queue message themselves — through the interaction for a button
        join, through _refresh_message for an admin !add."""
        users = list(self.queue.values())[:QUEUE_TARGET]
        self.queue.clear()
        return users

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
        roster_ids = {str(u.id) for u in users}
        self.sitting_out.clear()
        self.waitlist[:] = [uid for uid in self.waitlist if uid not in roster_ids]
        self.save_lineup()
        await self._redraw_lineup()  # the summary still shows the old lineup
        if promoted and any(self.queue.pop(str(u.id), None) for u in promoted):
            await self._refresh_message()
        view = ProposedMatchView(self, list(users), options)
        pinged = list(users) if announce else list(promoted)
        content = None
        if announce:
            content = " ".join(f"<@{u.id}>" for u in users) + " — your match is ready!"
        elif promoted:
            content = " ".join(f"<@{u.id}>" for u in promoted) + " — a spot opened up, you're in!"
        message = await channel.send(
            content=content,
            embed=view.embed(),
            view=view,
            allowed_mentions=discord.AllowedMentions(users=pinged),
        )
        self.match_view = view
        await self._clear_match_message(keep=message)

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
        return NextGameView(self) if played * 2 >= len(roster) else None

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


async def setup(client):
    await client.add_cog(Matchmaking(client))
