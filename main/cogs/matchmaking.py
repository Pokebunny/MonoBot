"""Matchmaking queue: players join, and when the queue fills the bot splits
them into the two most balanced teams using their skill ratings.

The queue is a single in-memory roster (one queue for the bot). discord.py
runs interaction callbacks on one event-loop thread, so no locking is needed.
"""

import datetime as dt
import logging
import zoneinfo

import discord
from checks import is_bot_admin
from discord.ext import commands, tasks
from models.matchmaking import ProposedMatch, QueuedPlayer
from resources.config import CONFIG
from services import identity, match_embeds
from services.matchmaking import afk_due, next_roster, ranked_matches
from services.rating import DEFAULT_MU, DEFAULT_SIGMA, RatingCache
from services.storage import MatchStore
from views import PersonPickView

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

# How often the AFK sweep looks for idle players and unanswered checks. Sets
# the slack on both timers, so keep it small next to the grace period.
AFK_SWEEP_SECONDS = 30


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


class AfkCheckView(discord.ui.View):
    """The "still here?" button sent to one idle player. Not persistent: the
    queue lives in memory, so after a restart there's nothing left to confirm."""

    def __init__(self, cog: "Matchmaking", uid: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.uid = uid

    @discord.ui.button(label="I'm still here", style=discord.ButtonStyle.success, emoji="✋")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.uid:
            await interaction.response.send_message("This check isn't for you.", ephemeral=True)
            return
        if self.uid not in self.cog.queue:
            await interaction.response.send_message("You're no longer in the queue.", ephemeral=True)
            await self.cog.delete_quietly(interaction.message)
            return
        await interaction.response.send_message("Thanks — you're still in the queue.", ephemeral=True)
        # Restarts their clock and deletes this message.
        await self.cog.mark_active(self.uid)


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
        lineup = self.cog.lineup_for_next(interaction.guild, users)
        if isinstance(lineup, str):
            await interaction.response.send_message(lineup, ephemeral=True)
            return
        users, promoted = lineup
        await interaction.response.defer()
        await self.cog.post_match(interaction.channel, users, promoted=promoted)
        if drop_message:
            try:
                await interaction.message.delete()
            except discord.HTTPException:
                pass

    @discord.ui.button(
        label="Sit out", style=discord.ButtonStyle.secondary, emoji="🪑", custom_id="monobot:match:sitout"
    )
    async def sit_out(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = str(interaction.user.id)
        if uid not in self.player_ids:
            await interaction.response.send_message(
                "Only a player in this match can sit out — press **Waitlist** to wait for a spot.", ephemeral=True
            )
            return
        if uid in self.cog.sitting_out:
            self.cog.sitting_out.remove(uid)
            reply = "You're back in for the next game."
        else:
            self.cog.sitting_out.append(uid)
            reply = "You'll sit out the next game; the first person on the waitlist takes your spot. Press again to change your mind."
        await self._show_lineup_change(interaction, reply)

    @discord.ui.button(
        label="Waitlist", style=discord.ButtonStyle.secondary, emoji="⏳", custom_id="monobot:match:waitlist"
    )
    async def waitlist(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = str(interaction.user.id)
        if uid in self.player_ids:
            await interaction.response.send_message(
                "You're already in this match — press **Sit out** to give up your spot.", ephemeral=True
            )
            return
        if uid in self.cog.waitlist:
            self.cog.waitlist.remove(uid)
            reply = "You've left the waitlist."
        elif not self.cog.store.sc2_names_for(uid):
            await interaction.response.send_message(
                "You need to link your SC2 name before you can play. Run `!link <your SC2 name>` first.",
                ephemeral=True,
            )
            return
        else:
            self.cog.waitlist.append(uid)
            reply = (
                f"You're #{len(self.cog.waitlist)} on the waitlist — you'll be pinged when a spot opens. "
                "Press again to leave."
            )
        await self._show_lineup_change(interaction, reply)

    async def _show_lineup_change(self, interaction: discord.Interaction, reply: str):
        """Save a sit-out / waitlist change and redraw it on the message. Reads
        the embed back off the message rather than rebuilding it, so this works
        on a proposal restored after a restart too."""
        self.cog.save_lineup()
        await interaction.response.send_message(reply, ephemeral=True)
        try:
            await interaction.message.edit(embed=self.cog.lineup_embed(interaction.message.embeds[0]))
        except discord.HTTPException, IndexError:
            pass


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
        # AFK check state. Each queued player's last sign of life (joining, or
        # answering a check), and the checks still awaiting an answer: their
        # deadline and the message carrying the button (None if it couldn't
        # be delivered — the deadline still stands).
        self.confirmed_at: dict[str, dt.datetime] = {}
        self.afk_checks: dict[str, tuple[dt.datetime, discord.Message | None]] = {}
        # Spots changing hands at the next re-team (see ProposedMatchView).
        self.sitting_out: list[str] = self._stored_ids(SITTING_OUT_META_KEY)
        self.waitlist: list[str] = self._stored_ids(WAITLIST_META_KEY)

    async def cog_load(self):
        # Register the persistent view so Join/Leave buttons on queue messages
        # from before the last restart still dispatch here.
        self.client.add_view(QueueView(self))
        # Same for New teams on a proposal that outlived the restart.
        self.client.add_view(ProposedMatchView(self))
        reset_at = _reset_time()
        if reset_at is not None:
            self.daily_reset.change_interval(time=reset_at)
            self.daily_reset.start()
        if CONFIG.afk_check_minutes:
            self.afk_sweep.start()

    async def cog_unload(self):
        self.daily_reset.cancel()
        self.afk_sweep.cancel()

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

    # -- AFK check ---------------------------------------------------------

    # The daily reset catches a queue forgotten overnight; this catches one
    # player who wandered off mid-session and would otherwise be pinged into
    # a match they aren't there for.
    @tasks.loop(seconds=AFK_SWEEP_SECONDS)
    async def afk_sweep(self):
        await self._forget_departed()
        now = discord.utils.utcnow()
        to_check, to_remove = afk_due(
            self.confirmed_at,
            {uid: deadline for uid, (deadline, _) in self.afk_checks.items()},
            now,
            dt.timedelta(minutes=CONFIG.afk_check_minutes),
        )
        for uid in to_remove:
            await self._remove_afk(uid)
        if to_remove:
            await self._refresh_message()
        for uid in to_check:
            await self._send_afk_check(uid, now + dt.timedelta(minutes=CONFIG.afk_check_grace_minutes))

    @afk_sweep.before_loop
    async def before_afk_sweep(self):
        await self.client.wait_until_ready()

    @afk_sweep.error
    async def afk_sweep_error(self, error):
        # A loop task dies on an unhandled exception; log it and keep sweeping.
        logger.exception("AFK sweep failed", exc_info=error)
        self.afk_sweep.restart()

    async def mark_active(self, uid: str):
        """Restart a player's AFK clock, withdrawing any check they have out.
        Called on joining and on answering a check."""
        self.confirmed_at[uid] = discord.utils.utcnow()
        _, message = self.afk_checks.pop(uid, (None, None))
        await self.delete_quietly(message)

    async def _forget_departed(self):
        """Drop AFK state for players no longer queued — they left, were
        bumped, or got a match — and take down any check they had out. Done
        here rather than at every place the queue shrinks."""
        for uid in [uid for uid in self.confirmed_at if uid not in self.queue]:
            del self.confirmed_at[uid]
        for uid in [uid for uid in self.afk_checks if uid not in self.queue]:
            _, message = self.afk_checks.pop(uid)
            await self.delete_quietly(message)

    async def _send_afk_check(self, uid: str, deadline: dt.datetime):
        """Ask one idle player to confirm, in the queue's channel so the ping
        lands where they queued; by DM if there's no live queue message."""
        content = (
            f"<@{uid}> are you still there? You've been in the queue for "
            f"{CONFIG.afk_check_minutes} minutes — press the button "
            f"{discord.utils.format_dt(deadline, 'R')} or you'll be removed."
        )
        message = None
        try:
            if self.queue_message is not None:
                message = await self.queue_message.channel.send(
                    content,
                    view=AfkCheckView(self, uid),
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            else:
                message = await self.queue[uid].send(content, view=AfkCheckView(self, uid))
        except discord.HTTPException:
            logger.warning("Couldn't deliver AFK check to %s; removing at deadline anyway", uid)
        self.afk_checks[uid] = (deadline, message)

    async def _remove_afk(self, uid: str):
        """Take a player who let their check lapse out of the queue, and turn
        the check into a note saying why, so they aren't left wondering."""
        user = self.queue.pop(uid, None)
        self.confirmed_at.pop(uid, None)
        _, message = self.afk_checks.pop(uid, (None, None))
        logger.info("AFK check: removed %s from the queue", user.display_name if user is not None else uid)
        if message is not None:
            try:
                await message.edit(
                    content=f"<@{uid}> was removed from the queue for not answering the AFK check.",
                    view=None,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass

    @staticmethod
    async def delete_quietly(message: discord.Message | None):
        if message is None:
            return
        try:
            await message.delete()
        except discord.HTTPException:
            pass

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
        return match_embeds.queue_status(self._players(), QUEUE_TARGET)

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
        """A guild member from a typed name, resolved the same way every other
        command resolves names: their claimed SC2 name first, then an account's
        current in-game name, then an exact Discord name/nickname/mention/id.
        No partial matching — queueing the wrong person is worse than being
        told to type the whole name.

        None when the caller has already been answered: nothing matched, the
        name is shared and a picker went out, or the person isn't reachable."""
        people = identity.resolve(self.store, query)
        if identity.ambiguous(people):
            view = PersonPickView(people, str(ctx.author.id), on_pick)
            view.message = await ctx.send(f"More than one player has played as **{query}** — which one?", view=view)
            return None
        if people:
            member = await self._member_of(ctx, people[0])
            if member is not None:
                return member
        try:  # a Discord handle, mention or id rather than an SC2 name
            return await commands.MemberConverter().convert(ctx, query)
        except commands.BadArgument:
            pass
        if people and people[0].discord_id:
            await ctx.send(f"**{people[0].sc2_name}** is linked, but isn't in this server.")
        elif people:
            await ctx.send(
                f"**{people[0].sc2_name}** hasn't linked a Discord account yet — "
                "they need to run `!link <their SC2 name>` before they can queue."
            )
        else:
            await ctx.send(f"No player found matching **{query}**.")
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
            await self._redraw_match_message()
        return f"Removed **{label}** from the {'queue' if queued else 'waitlist'}."

    @commands.hybrid_command(help="re-post the last match with freshly balanced teams (optionally naming a new roster)")
    @commands.cooldown(1, 10, commands.BucketType.channel)
    async def teams(self, ctx, members: commands.Greedy[discord.Member] = None):
        """Re-split a roster without going back through the queue. With no
        arguments it re-teams whoever was in the last match, which is the
        common case after a few games; name members to swap someone in or out."""
        roster = list(dict.fromkeys(members or self.last_roster))
        if not roster:
            await ctx.send("No recent match to re-team — run `!queue` to start one.")
            return
        promoted = []
        if not members:  # a named roster is taken as given; sit-outs only apply to the last one
            lineup = self.lineup_for_next(ctx.guild, roster)
            if isinstance(lineup, str):
                await ctx.send(lineup)
                return
            roster, promoted = lineup
        if len(roster) < 2 or len(roster) % 2 != 0:
            await ctx.send(f"Need an even number of players, got {len(roster)}.")
            return
        await self.post_match(ctx.channel, roster, promoted=promoted)

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
            message, roster = await self._add(member)
            await interaction.response.edit_message(content=message, view=None)
            await self._refresh_message()
            if roster:
                await self.post_match(ctx.channel, roster, announce=True)

        member = await self._member_for(ctx, player, picked)
        if member is None:
            return
        message, roster = await self._add(member)
        await self._refresh_message()
        await ctx.send(message)
        if roster:
            await self.post_match(ctx.channel, roster, announce=True)

    async def _add(self, member) -> tuple[str, list | None]:
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
            return f"**{member.display_name}** is already in the queue.", None
        self.queue[uid] = member
        await self.mark_active(uid)
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
            await interaction.response.send_message("You're already in the queue.", ephemeral=True)
            return
        self.queue[uid] = interaction.user
        await self.mark_active(uid)
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
        await self._clear_match_message(keep=message)

    # -- sit-outs & waitlist ----------------------------------------------

    def _stored_ids(self, key: str) -> list[str]:
        return [i for i in (self.store.get_meta(key) or "").split(",") if i]

    def save_lineup(self):
        self.store.set_meta(SITTING_OUT_META_KEY, ",".join(self.sitting_out))
        self.store.set_meta(WAITLIST_META_KEY, ",".join(self.waitlist))

    def lineup_embed(self, embed: discord.Embed) -> discord.Embed:
        return match_embeds.with_lineup_changes(embed, self.sitting_out, self.waitlist)

    async def _redraw_match_message(self):
        """Redraw the lineup fields on the live proposal after a change made
        away from its buttons (e.g. !bump)."""
        if self.match_message is None or not self.match_message.embeds:
            return
        try:
            await self.match_message.edit(embed=self.lineup_embed(self.match_message.embeds[0]))
        except discord.HTTPException:
            pass

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
                f"need {short} more. Press **Waitlist** to take a spot, press **Sit out** again to stay in, "
                "or name a roster with `!teams`."
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
