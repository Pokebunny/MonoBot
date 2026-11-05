"""Communities: one ladder per Discord server (see CLAUDE.md). Unconfigured
servers get none, so an invite doesn't quietly start a ladder."""

import datetime as dt
import logging
import os
import zoneinfo
from typing import Self

import discord
from discord.ext import commands, tasks
from models.config import BotConfig, GuildConfig
from services.achievements import AchievementCache
from services.rating import DuoCache, PickCache, RatingCache, UnitCache
from services.storage import DEFAULT_DB_PATH, MatchStore, SharedLinks

logger = logging.getLogger(__name__)

GUILDS_DIR = os.path.join(os.path.dirname(__file__), "resources", "guilds")


class Community:
    def __init__(self, guild_id: int | None, config: GuildConfig, store: MatchStore):
        self.guild_id = guild_id  # None for a legacy home that serves every server
        self.config = config
        self.store = store
        self.ratings = RatingCache(store)
        self.achievements = AchievementCache(store)
        self.duos = DuoCache(store)
        self.units = UnitCache(store)
        self.pick_boards = PickCache(store)

    def watched_channels(self) -> set[int]:
        return set(self.config.replays_channel_ids)

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.config.admin_user_ids

    def owns(self, channel) -> bool:
        """Whether a channel is in this community's server, so a misfiled channel
        id can't leak one ladder into another."""
        guild = getattr(channel, "guild", None)
        return self.guild_id is None or (guild is not None and guild.id == self.guild_id)

    def post_channel(self, client):
        """Its first visible replay channel, for unprompted posts."""
        for channel_id in sorted(self.watched_channels()):
            channel = client.get_channel(channel_id)
            if channel is not None and self.owns(channel):
                return channel
        return None

    def config_problems(self, client) -> list[str]:
        """Config mistakes visible from Discord (missing server or channels)."""
        if self.guild_id is None:
            return []
        if client.get_guild(self.guild_id) is None:
            return [f"the bot isn't in server {self.guild_id} — invite it"]
        problems = []
        for channel_id in sorted(self.watched_channels()):
            channel = client.get_channel(channel_id)
            if channel is None:
                problems.append(f"can't see replay channel {channel_id}")
            elif not self.owns(channel):
                problems.append(f"replay channel {channel_id} is in another server ({channel.guild.id})")
        return problems


class Communities:
    def __init__(self, config: BotConfig, guilds_dir: str = GUILDS_DIR):
        self.config = config
        links = SharedLinks(DEFAULT_DB_PATH)
        self.home = Community(config.home_guild_id, config, MatchStore(DEFAULT_DB_PATH, links))
        self.others = {
            guild_id: Community(
                guild_id, guild_config, MatchStore(os.path.join(guilds_dir, str(guild_id), "monobot.db"), links)
            )
            for guild_id, guild_config in config.guilds.items()
        }

    def all(self) -> list[Community]:
        return [self.home, *self.others.values()]

    def for_guild(self, guild: discord.abc.Snowflake | int | None) -> Community | None:
        """None if the server has none; a DM maps to home only when it's the only one."""
        if guild is None:
            return None if self.others else self.home
        guild_id = guild if isinstance(guild, int) else guild.id
        if guild_id in self.others:
            return self.others[guild_id]
        if self.config.home_guild_id in (None, guild_id):
            return self.home
        return None

    def collectors_of(self, community: Community) -> list[Community]:
        """Communities whose `counts_games_from` lists this one."""
        if community.guild_id is None:
            return []
        return [c for c in self.all() if c is not community and community.guild_id in c.config.counts_games_from]

    def sharing_games_with(self, community: Community) -> list[Community]:
        """Collectors and sources: where a confirmed winner is copied to."""
        sources = [c for c in self.all() if c is not community and c.guild_id in community.config.counts_games_from]
        return [*self.collectors_of(community), *(c for c in sources if c not in self.collectors_of(community))]

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.config.admin_user_ids


class NotACommunity(commands.CheckFailure):
    def __init__(self, ctx):
        where = "in DMs" if ctx.guild is None else "in this server"
        hint = " — use it in your server" if ctx.guild is None else ""
        super().__init__(f"MonoBot's ladder isn't set up {where}{hint}.")


class CommunityCog(commands.Cog):
    """A cog over one community's data. Commands start with `self = self.at(ctx)`;
    forgetting it fails loudly (the unbound cog has no `store`)."""

    community: Community

    def __init__(self, client):
        self.client = client

    def at(self, where) -> Self:
        """A copy bound to a Community or a ctx/interaction/guild's one. Skips
        Cog.__new__, which re-copies every command."""
        community = where if isinstance(where, Community) else self.client.communities.for_guild(_guild_of(where))
        bound = object.__new__(type(self))
        bound.__dict__.update(self.__dict__)
        bound.community = community
        bound.store = community.store
        bound.ratings = community.ratings
        bound.achievements = community.achievements
        bound.duos = community.duos
        bound.units = community.units
        bound.pick_boards = community.pick_boards
        return bound

    async def cog_check(self, ctx) -> bool:
        if self.client.communities.for_guild(ctx.guild) is None:
            raise NotACommunity(ctx)
        return True


def _guild_of(where):
    if where is None or isinstance(where, (int, discord.Guild)):
        return where
    return where.guild  # a Context, Interaction, Message or channel


def daily_time(raw: str | None, tz_name: str, what: str) -> dt.time | None:
    """ "HH:MM" in an IANA timezone; None if disabled or unparseable."""
    if not raw:
        return None
    try:
        hour, minute = (int(part) for part in raw.split(":"))
        return dt.time(hour=hour, minute=minute, tzinfo=zoneinfo.ZoneInfo(tz_name))
    except ValueError, zoneinfo.ZoneInfoNotFoundError:
        logger.warning("Ignoring bad %s config (%r in %r); disabled", what, raw, tz_name)
        return None


def start_daily(client, when, job, what: str) -> list[tasks.Loop]:
    """Run `job(community)` daily at `when(config)`, one loop per community
    (tasks.loop dedupes times by wall clock across timezones). Cancel the
    returned loops on unload."""
    loops = []
    for community in client.communities.all():
        at = when(community.config)
        if at is None:
            continue

        async def run(community=community):
            # An unhandled error would end the loop for good.
            try:
                await job(community)
            except Exception:
                logger.exception("%s failed for community %s", what, community.guild_id)

        loop = tasks.loop(time=at)(run)
        loop.before_loop(client.wait_until_ready)
        loop.start()
        loops.append(loop)
    return loops
