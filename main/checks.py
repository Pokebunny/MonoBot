"""Shared command checks."""

from discord.ext import commands


def is_bot_admin():
    """Passes for configured bot admins (by user id: the top-level list
    anywhere, a community's own list in its server) OR anyone with Discord's
    Manage Server permission — so a bot admin needs no server permissions."""

    async def predicate(ctx):
        communities = ctx.bot.communities
        if communities.is_admin(ctx.author.id):
            return True
        community = communities.for_guild(ctx.guild)
        if community is not None and community.is_admin(ctx.author.id):
            return True
        perms = getattr(ctx.author, "guild_permissions", None)
        return bool(perms and perms.manage_guild)

    return commands.check(predicate)
