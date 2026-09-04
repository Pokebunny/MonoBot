"""Shared discord.ui view behavior.

Interactive components fall into two camps here: the queue's Join/Leave view
is persistent (fixed custom_ids, registered with client.add_view, survives
restarts), and everything else expires. ExpiringView is the base for the
latter: buttons stay clickable for 24 hours, then grey out so stale messages
don't show clickable-but-dead components.
"""

import logging

import discord
from services.identity import Person

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


class PagedBoardView(ExpiringView):
    """⏮ ◀ ▶ ⏭ pagination for any board embed.

    `render(page)` returns the embed for that page and closes over the board
    data, so the ranking is snapshotted at send time and paging stays
    consistent even if a game is uploaded mid-browse. Any board that can
    render a page — ratings, MVP rate — pages the same way through this."""

    def __init__(self, render, pages: int):
        super().__init__()
        self.render = render
        self.pages = max(1, pages)
        self.page = 0
        self._sync()

    @property
    def multipage(self) -> bool:
        return self.pages > 1

    def embed(self) -> discord.Embed:
        return self.render(self.page)

    def _sync(self):
        at_start = self.page <= 0
        at_end = self.page >= self.pages - 1
        self.first.disabled = self.prev.disabled = at_start
        self.next.disabled = self.last.disabled = at_end

    async def _show(self, interaction: discord.Interaction):
        self._sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(emoji="⏮", style=discord.ButtonStyle.secondary)
    async def first(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = 0
        await self._show(interaction)

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self._show(interaction)

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.pages - 1, self.page + 1)
        await self._show(interaction)

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary)
    async def last(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = self.pages - 1
        await self._show(interaction)


class PersonPickView(ExpiringView):
    """Ask which player was meant, when a name is genuinely shared.

    Only shown for real ambiguity — two accounts matching on the SAME strength
    of evidence (see services.identity.ambiguous). A claimed name that also
    happens to be some other account's old alias is NOT ambiguous, and asking
    there would be noise.

    `on_pick(interaction, person)` continues whatever the caller was doing."""

    def __init__(self, people: list[Person], invoker_id: str, on_pick):
        super().__init__()
        self.add_item(_PersonSelect(people, invoker_id, on_pick))


class _PersonSelect(discord.ui.Select):
    def __init__(self, people: list[Person], invoker_id: str, on_pick):
        self.people = {str(i): p for i, p in enumerate(people[:25])}
        self.invoker_id = invoker_id
        self.on_pick = on_pick
        options = [
            discord.SelectOption(
                label=p.sc2_name[:100],
                description=f"{p.games} games · {'linked' if p.discord_id else 'unlinked'} · …{p.handles[-1][-6:]}"
                if p.handles
                else f"{p.games} games",
                value=key,
            )
            for key, p in self.people.items()
        ]
        super().__init__(placeholder="Which player?", options=options)

    async def callback(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.invoker_id:
            await interaction.response.send_message("Only the person who ran this command can choose.", ephemeral=True)
            return
        self.view.stop()
        await self.on_pick(interaction, self.people[self.values[0]])
