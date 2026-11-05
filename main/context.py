"""Command context: slash invocations reply privately, `!` ones publicly."""

from discord.ext import commands

# Commands others must see or click (canonical names): always public.
ALWAYS_PUBLIC = {"queue", "waitlist"}


class MonoContext(commands.Context):
    def private_by_default(self) -> bool:
        return not (self.command is not None and self.command.name in ALWAYS_PUBLIC)

    async def defer_slash(self):
        """Defer a slash invocation before slow work (Discord's 3-second limit).
        The first reply after a defer takes the defer's visibility, so this
        defers with the same default send uses."""
        if self.interaction is not None and not self.interaction.response.is_done():
            await self.defer(ephemeral=self.private_by_default())

    async def send(self, *args, **kwargs):
        if self.interaction is not None and "ephemeral" not in kwargs:
            kwargs["ephemeral"] = self.private_by_default()
        return await super().send(*args, **kwargs)
