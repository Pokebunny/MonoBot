"""Argument converters shared by the cogs.

discord.py matches a typed member name with `==`, so `!teams bob` misses a
member whose nickname is "Bob". `MemberConverter` keeps the stock lookup (ids,
mentions, exact names) and only falls back to ignoring case when that finds
nobody — an exact-case match always wins. A name that fits several members
ignoring case raises `AmbiguousMember`, which carries them so a command can
put up the same player picker an ambiguous SC2 name gets.

`install()` swaps it in for every `discord.Member` parameter, so commands
annotate with plain `discord.Member` and slash commands keep Discord's native
member picker (which never goes through a converter).
"""

import discord
from discord.ext import commands
from discord.ext.commands import converter as _converter


class AmbiguousMember(commands.MemberNotFound):
    """Several members are called this, ignoring case. A MemberNotFound, so
    commands that don't offer a picker (`!teams`, `!linkuser`) just report it."""

    def __init__(self, argument: str, members: list[discord.Member]):
        self.argument = argument
        self.members = members
        commands.BadArgument.__init__(self, f'More than one member is called "{argument}" — @mention them instead.')


class MemberConverter(commands.MemberConverter):
    async def convert(self, ctx, argument: str) -> discord.Member:
        try:
            return await super().convert(ctx, argument)
        except commands.MemberNotFound:
            members = members_named_ignoring_case(ctx.guild, argument)
            if len(members) > 1:
                raise AmbiguousMember(argument, members)
            if not members:
                raise
            return members[0]


def members_named_ignoring_case(guild: discord.Guild | None, name: str) -> list[discord.Member]:
    """Members called `name` in any case. Usernames are unique, so a username
    match is the answer on its own; display names and nicknames are not, so
    those can return several."""
    if guild is None:
        return []
    wanted = name.strip().casefold()
    if not wanted:
        return []
    by_username = [m for m in guild.members if m.name.casefold() == wanted]
    if by_username:
        return by_username
    return [m for m in guild.members if wanted in {(m.global_name or "").casefold(), (m.nick or "").casefold()}]


def install() -> None:
    """Resolve every `discord.Member` argument through MemberConverter."""
    _converter.CONVERTER_MAPPING[discord.Member] = MemberConverter
