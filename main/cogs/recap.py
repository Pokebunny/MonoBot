"""Session recaps: !recap for the latest session, plus a morning post
summarising the previous day's games."""

import datetime as dt
import logging
import zoneinfo

import discord
from community import CommunityCog, daily_time, start_daily
from discord.ext import commands
from services import match_embeds, recap
from services.rating import season_start
from views import PagedBoardView

logger = logging.getLogger(__name__)

# The morning post covers this much history before it, so a session that runs
# past midnight is reported whole.
DAILY_WINDOW = dt.timedelta(hours=24)


def _recap_timezone(config) -> dt.tzinfo:
    try:
        return zoneinfo.ZoneInfo(config.recap_timezone)
    except ValueError, zoneinfo.ZoneInfoNotFoundError:
        logger.warning("Bad recap_timezone %r; using UTC", config.recap_timezone)
        return dt.UTC


def _recap_time(config) -> dt.time | None:
    """A community's morning-post time, or None if it's disabled."""
    return daily_time(config.recap_time, config.recap_timezone, "recap_time")


class Recap(CommunityCog):
    async def cog_load(self):
        self.daily_recaps = start_daily(
            self.client,
            _recap_time,
            lambda community: self.at(community).post_daily_recap(),
            "Morning recap",
        )

    async def cog_unload(self):
        for loop in self.daily_recaps:
            loop.cancel()

    @commands.hybrid_command(
        aliases=["session", "nightly"],
        description="recap the latest session, or a past one by its number (!recap 12)",
        help="recap the latest session: records, rating changes, MVPs and achievements (◀ for older sessions). "
        "Give a session number (shown in the footer) to post just that one, without page buttons",
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def recap(self, ctx, session: int | None = None):
        self = self.at(ctx)
        # Sessions with no decided game have nothing to report, so they get
        # no page rather than a blank one.
        sessions = [s for s in recap.split_sessions(self.store.all_matches()) if any(recap.is_decided(m) for _, m in s)]
        if not sessions:
            await ctx.send("No decided games to recap yet.")
            return
        if session is not None:
            # One fixed session: a static post that reads the same later,
            # numbered as the paged recap's footer numbers it.
            if not 1 <= session <= len(sessions):
                await ctx.send(f"There's no session {session} — sessions run from 1 to {len(sessions)}.")
                return
            embed = self._embed(sessions[session - 1], "📋 Session Recap", ctx.guild)
            embed.set_footer(text=f"Session {session}/{len(sessions)} · {embed.footer.text}")
            await ctx.send(embed=embed)
            return
        # Each page is a season rating walk, so build them as they're opened.
        cache: dict[int, discord.Embed] = {}

        def render(page: int) -> discord.Embed:
            if page not in cache:
                embed = self._embed(sessions[page], "📋 Session Recap", ctx.guild)
                embed.set_footer(text=f"Session {page + 1}/{len(sessions)} · {embed.footer.text}")
                cache[page] = embed
            return cache[page]

        # Open on the latest session; ◀ steps back in time, as in !matches.
        view = PagedBoardView(render, len(sessions), start=len(sessions) - 1)
        view.message = await ctx.send(embed=view.embed(), view=view)

    async def post_daily_recap(self):
        now = dt.datetime.now(dt.UTC)
        since = (now - DAILY_WINDOW).isoformat()
        session = self.store.all_matches(since=since)
        channels = [
            c
            # The replay channels: that's where the night's games were posted.
            for c in (self.client.get_channel(cid) for cid in sorted(self.community.watched_channels()))
            if c is not None and self.community.owns(c)
        ]
        if not channels:
            logger.warning("Morning recap: no reachable channel to post in")
            return
        for channel in channels:
            embed = self._embed(session, "☀️ Last Night's Recap", getattr(channel, "guild", None))
            if embed is None:
                logger.info("Morning recap: no decided games in the last %s", DAILY_WINDOW)
                return
            try:
                await channel.send(embed=embed)
            except discord.HTTPException:
                logger.exception("Morning recap: failed to post in %s", channel)

    def _embed(self, session, title: str, guild) -> discord.Embed | None:
        if not session:
            return None
        first, last = session[0][1].played_at, session[-1][1].played_at
        season = self.store.season_containing(first.isoformat()) or self.store.current_season()
        summary = recap.build_recap(
            session,
            self.store.season_matches(season),
            self.store.merge_map(),
            self.store.unlocks_between(first, last),
            start=season_start(self.store, season),
        )
        if summary is None:
            return None
        names = {p.handle: self._shown_name(guild, p.handle, p.name) for p in summary.players}
        return match_embeds.session_recap(summary, names, _recap_timezone(self.community.config), title)

    def _shown_name(self, guild, handle: str, fallback: str) -> str:
        """The linked member's Discord name, else the SC2 name — the same
        rule the leaderboard uses."""
        for h in self.store.merged_handles(handle):
            discord_id = self.store.discord_id_for_handle(h)
            if discord_id is None:
                continue
            member = guild.get_member(int(discord_id)) if guild else None
            user = member or self.client.get_user(int(discord_id))
            return user.display_name if user else fallback
        return fallback


async def setup(client):
    await client.add_cog(Recap(client))
