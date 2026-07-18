"""Shared discord.ui view behavior.

Interactive components fall into two camps here: the queue's Join/Leave view
is persistent (fixed custom_ids, registered with client.add_view, survives
restarts), and everything else expires. ExpiringView is the base for the
latter: buttons stay clickable for 24 hours, then grey out so stale messages
don't show clickable-but-dead components.
"""

import logging

import discord

logger = logging.getLogger(__name__)

VIEW_TIMEOUT_SECONDS = 24 * 60 * 60


class ExpiringView(discord.ui.View):
    """A view whose components are disabled in place when it times out.
    Callers must assign .message after sending (or leave it None for a view
    that was never attached to a message). Completion paths that edit the
    view away should call stop() so the timeout edit never fires."""

    def __init__(self, timeout: float = VIEW_TIMEOUT_SECONDS):
        super().__init__(timeout=timeout)
        self.message: discord.Message | None = None

    async def on_timeout(self):
        if self.message is None:
            return
        for item in self.children:
            item.disabled = True
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass  # message deleted, or we lost permission — nothing to grey out
