"""A case-insensitive fallback for discord.py's exact-match member lookup.
An exact-case match still wins; several case-insensitive matches raise
`AmbiguousMember` so commands can offer a picker.
"""

import discord
from discord.ext import commands
from discord.ext.commands import converter as _converter


class AmbiguousMember(commands.MemberNotFound):
    """A MemberNotFound, so commands without a picker just report it."""

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
    """A unique username match wins over (possibly shared) display names."""
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
