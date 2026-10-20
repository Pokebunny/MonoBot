"""Shared command checks."""

from discord.ext import commands


def is_admin(bot, guild, user) -> bool:
    """Configured bot admins (by user id: the top-level list anywhere, a
    community's own list in its server) OR anyone with Discord's Manage
    Server permission — so a bot admin needs no server permissions."""
    communities = bot.communities
    if communities.is_admin(user.id):
        return True
    community = communities.for_guild(guild)
    if community is not None and community.is_admin(user.id):
        return True
    perms = getattr(user, "guild_permissions", None)
    return bool(perms and perms.manage_guild)


def is_bot_admin():
    """Command check for is_admin."""

    async def predicate(ctx):
        return is_admin(ctx.bot, ctx.guild, ctx.author)

    return commands.check(predicate)
