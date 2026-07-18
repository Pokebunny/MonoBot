"""Shared command checks."""

from discord.ext import commands
from resources.config import CONFIG


def is_bot_admin():
    """Passes for configured bot admins (by user id) OR anyone with Discord's
    Manage Server permission — so a bot admin needs no server permissions."""

    async def predicate(ctx):
        if ctx.author.id in CONFIG.admin_user_ids:
            return True
        perms = getattr(ctx.author, "guild_permissions", None)
        return bool(perms and perms.manage_guild)

    return commands.check(predicate)
