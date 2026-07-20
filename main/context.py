# file: context.py

"""Custom command context that decides response visibility from *how* a hybrid
command was invoked.

Convention: a slash invocation (`/rank`) is a private peek — the reply is
ephemeral, visible only to the caller — while the text form (`!rank`) posts
publicly to the channel. This lets people check their own standing quietly or
share it deliberately, using the same command.

Commands whose output others must *see or interact with* (the queue message,
whose Join/Leave buttons are clicked by everyone) opt out via ALWAYS_PUBLIC and
stay public regardless of how they were invoked.
"""

from discord.ext import commands

# Canonical command names (not aliases) whose reply must stay public even when
# invoked as a slash command, because other members act on the message itself.
ALWAYS_PUBLIC = {"queue"}


class MonoContext(commands.Context):
    async def send(self, *args, **kwargs):
        # Only default the visibility for slash invocations, and never override
        # an explicit ephemeral= the caller passed (e.g. /gallery's private
        # secret-recipe reveal). Text (prefix) invocations have no interaction,
        # so ephemeral is meaningless and left untouched.
        if self.interaction is not None and "ephemeral" not in kwargs:
            public = self.command is not None and self.command.name in ALWAYS_PUBLIC
            kwargs["ephemeral"] = not public
        return await super().send(*args, **kwargs)
