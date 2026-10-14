"""Session recaps: !recap for the latest session, plus a morning post
summarising the previous day's games."""

import datetime as dt
import logging
import zoneinfo

import discord
from checks import is_bot_admin
from community import CommunityCog, daily_time, start_daily
from discord.ext import commands
from services import match_embeds, recap
from services.rating import season_start
from views import PagedBoardView

logger = logging.getLogger(__name__)

# meta key holding the channel the morning recap goes to. Unset means the
# replay channels — that's where the night's games were posted.
RECAP_CHANNEL_META_KEY = "recap_channel"

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
        help="recap the latest session: records, rating changes, MVPs and achievements (◀ for older sessions)",
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def recap(self, ctx):
        self = self.at(ctx)
        # Sessions with no decided game have nothing to report, so they get
        # no page rather than a blank one.
        sessions = [s for s in recap.split_sessions(self.store.all_matches()) if any(recap.is_decided(m) for _, m in s)]
        if not sessions:
            await ctx.send("No decided games to recap yet.")
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

    @commands.hybrid_command(
        help="send the morning recap to this channel (or #channel); run again to go back to the default (mods)"
    )
    @is_bot_admin()
    async def recapchannel(self, ctx, channel: discord.TextChannel | None = None):
        self = self.at(ctx)
        target = channel or ctx.channel
        if self.store.get_meta(RECAP_CHANNEL_META_KEY) == str(target.id):
            self.store.set_meta(RECAP_CHANNEL_META_KEY, "")
            await ctx.send(f"Morning recap no longer pinned to {target.mention}; it goes to the replay channels.")
            return
        self.store.set_meta(RECAP_CHANNEL_META_KEY, str(target.id))
        config = self.community.config
        when = (
            f"at {config.recap_time} {config.recap_timezone}"
            if _recap_time(config)
            else "(currently disabled in config)"
        )
        await ctx.send(f"The morning recap will be posted in {target.mention} {when}.")

    async def post_daily_recap(self):
        now = dt.datetime.now(dt.UTC)
        since = (now - DAILY_WINDOW).isoformat()
        session = self.store.all_matches(since=since)
        channels = [c for c in (self.client.get_channel(cid) for cid in self._recap_channel_ids()) if c is not None]
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

    def _recap_channel_ids(self) -> list[int]:
        pinned = self.store.get_meta(RECAP_CHANNEL_META_KEY)
        if pinned:
            return [int(pinned)]
        return sorted(self.community.watched_channels())

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
