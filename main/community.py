"""Communities: one ladder per Discord server.

A community is a server's own match database, the caches derived from it and
its slice of the config. Nothing is shared between communities — not matches,
ratings, links nor achievements — so someone who plays in two of them links
twice and has two unrelated ratings.

The home community is the original ladder and keeps its database at
resources/monobot.db; every other one lives under resources/guilds/<id>/.
Servers that aren't configured get no community, so inviting the bot
somewhere doesn't quietly start a ladder there.
"""

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
from services.storage import DEFAULT_DB_PATH, MatchStore

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
        """Channels watched for replays: the config file's. Empty = watch
        everywhere (dev default)."""
        return set(self.config.replays_channel_ids)

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.config.admin_user_ids

    def owns(self, channel) -> bool:
        """Whether a channel is in this community's server. The bot posts a
        community's announcements only there, so a channel id filed under the
        wrong server in config can't leak one ladder into another. A legacy
        home (no guild id) owns every channel."""
        guild = getattr(channel, "guild", None)
        return self.guild_id is None or (guild is not None and guild.id == self.guild_id)

    def config_problems(self, client) -> list[str]:
        """What's wrong with this community's config, as seen from Discord:
        a server the bot isn't in, or a configured channel it can't see or
        that belongs to another server."""
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
    """Every configured community, and which one a server belongs to."""

    def __init__(self, config: BotConfig, guilds_dir: str = GUILDS_DIR):
        self.config = config
        self.home = Community(config.home_guild_id, config, MatchStore(DEFAULT_DB_PATH))
        self.others = {
            guild_id: Community(
                guild_id, guild_config, MatchStore(os.path.join(guilds_dir, str(guild_id), "monobot.db"))
            )
            for guild_id, guild_config in config.guilds.items()
        }

    def all(self) -> list[Community]:
        return [self.home, *self.others.values()]

    def for_guild(self, guild: discord.abc.Snowflake | int | None) -> Community | None:
        """The community a server belongs to, or None if it has none. A DM
        (no server) only has one when there's no other community it could
        mean."""
        if guild is None:
            return None if self.others else self.home
        guild_id = guild if isinstance(guild, int) else guild.id
        if guild_id in self.others:
            return self.others[guild_id]
        if self.config.home_guild_id in (None, guild_id):
            return self.home
        return None

    def is_admin(self, user_id: int) -> bool:
        """Admin everywhere: the top-level admin_user_ids."""
        return user_id in self.config.admin_user_ids


class NotACommunity(commands.CheckFailure):
    def __init__(self, ctx):
        where = "in DMs" if ctx.guild is None else "in this server"
        hint = " — use it in your server" if ctx.guild is None else ""
        super().__init__(f"MonoBot's ladder isn't set up {where}{hint}.")


class CommunityCog(commands.Cog):
    """A cog whose commands act on one community's data.

    The cog itself belongs to no community; `at(...)` hands back a copy bound
    to one, with that community's store and caches as attributes. A command
    rebinds `self` to that copy as its first line:

        async def leaderboard(self, ctx, ...):
            self = self.at(ctx)

    so the rest of the command, its helpers and any view callback that closes
    over `self` all read the right server's data, and two servers' commands
    running at once can't see each other's. Forgetting the line fails loudly
    (the unbound cog has no `store`) rather than reading the wrong ladder.
    """

    community: Community

    def __init__(self, client):
        self.client = client

    def at(self, where) -> Self:
        """A copy bound to a Community, or to the one a ctx/interaction/guild
        belongs to. Copied without calling __new__: Cog.__new__ re-copies every
        command, which is wasted work for a per-command throwaway."""
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
    """A configured "HH:MM" in an IANA timezone, or None if it's disabled or
    unparseable (a bad value shouldn't stop the bot from booting)."""
    if not raw:
        return None
    try:
        hour, minute = (int(part) for part in raw.split(":"))
        return dt.time(hour=hour, minute=minute, tzinfo=zoneinfo.ZoneInfo(tz_name))
    except ValueError, zoneinfo.ZoneInfoNotFoundError:
        logger.warning("Ignoring bad %s config (%r in %r); disabled", what, raw, tz_name)
        return None


def start_daily(client, when, job, what: str) -> list[tasks.Loop]:
    """Run `job(community)` once a day at each community's own time,
    `when(config)` (None skips that community). A loop per community rather
    than one loop with many times: tasks.loop dedupes times by wall clock, so
    08:00 in two timezones would collapse into one. Cancel the returned loops
    on unload."""
    loops = []
    for community in client.communities.all():
        at = when(community.config)
        if at is None:
            continue

        async def run(community=community):
            # Caught here: an unhandled error would end the loop for good.
            try:
                await job(community)
            except Exception:
                logger.exception("%s failed for community %s", what, community.guild_id)

        loop = tasks.loop(time=at)(run)
        loop.before_loop(client.wait_until_ready)
        loop.start()
        loops.append(loop)
    return loops
