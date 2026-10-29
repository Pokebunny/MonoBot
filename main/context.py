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
ALWAYS_PUBLIC = {"queue", "waitlist"}


class MonoContext(commands.Context):
    def private_by_default(self) -> bool:
        return not (self.command is not None and self.command.name in ALWAYS_PUBLIC)

    async def defer_slash(self):
        """Acknowledge a slash invocation before the command body runs.

        Discord drops an interaction not answered within 3 seconds, and the
        stats commands walk the whole match history on the event loop. The
        first reply after a defer takes the DEFER's visibility, whatever it
        passes itself, so this defers with the same default send applies."""
        if self.interaction is not None and not self.interaction.response.is_done():
            await self.defer(ephemeral=self.private_by_default())

    async def send(self, *args, **kwargs):
        # Only default the visibility for slash invocations, and never override
        # an explicit ephemeral= the caller passed (e.g. /gallery's private
        # secret-recipe reveal). Text (prefix) invocations have no interaction,
        # so ephemeral is meaningless and left untouched.
        if self.interaction is not None and "ephemeral" not in kwargs:
            kwargs["ephemeral"] = self.private_by_default()
        return await super().send(*args, **kwargs)
