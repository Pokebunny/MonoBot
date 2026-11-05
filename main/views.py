"""Shared discord.ui views. Everything but the persistent lobby view expires."""

import logging

import discord
from converters import AmbiguousMember, MemberConverter
from discord.ext import commands
from services import identity
from services.identity import Person

logger = logging.getLogger(__name__)

VIEW_TIMEOUT_SECONDS = 12 * 60 * 60  # each live view pins its data in memory until then


class ExpiringView(discord.ui.View):
    """Greys out its components on timeout. Set .message after sending; call
    stop() when editing the view away."""

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
    """⏮ ◀ ▶ ⏭ paging. `render(page)` closes over a snapshot of the board, so
    paging stays consistent if a game lands mid-browse."""

    def __init__(self, render, pages: int, start: int = 0):
        super().__init__()
        self.render = render
        self.pages = max(1, pages)
        self.page = min(max(0, start), self.pages - 1)
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
    """Asks which person a shared name meant; `on_pick(interaction, person)`
    resumes the command."""

    def __init__(self, people: list[Person], invoker_id: str, on_pick):
        super().__init__()
        self.add_item(_PersonSelect(people, invoker_id, on_pick))


async def person_or_pick(ctx, store, query: str, on_pick) -> list[Person] | None:
    """Who a typed name means: SC2 names, then Discord members. Returns
    everyone matched, best first, or None when already answered (no match, or
    a picker went out and `on_pick` will resume)."""
    people = identity.resolve(store, query)
    if identity.ambiguous(people):
        await _ask(ctx, people, f"More than one player has played as **{query}** — which one?", on_pick)
        return None
    if people:
        return people
    try:
        member = await MemberConverter().convert(ctx, query)
    except AmbiguousMember as err:
        people = [identity.for_discord(store, str(m.id), m.display_name) for m in err.members]
        await _ask(ctx, people, f"More than one member is called **{query}** — which one?", on_pick)
        return None
    except commands.BadArgument:
        await ctx.send(f"No player found matching **{query}**.", allowed_mentions=discord.AllowedMentions.none())
        return None
    return [identity.for_discord(store, str(member.id), member.display_name)]


async def _ask(ctx, people: list[Person], prompt: str, on_pick) -> None:
    view = PersonPickView(people, str(ctx.author.id), on_pick)
    view.message = await ctx.send(prompt, view=view, allowed_mentions=discord.AllowedMentions.none())


class _PersonSelect(discord.ui.Select):
    def __init__(self, people: list[Person], invoker_id: str, on_pick):
        self.people = {str(i): p for i, p in enumerate(people[:25])}
        self.invoker_id = invoker_id
        self.on_pick = on_pick
        options = [discord.SelectOption(**_option(p), value=key) for key, p in self.people.items()]
        super().__init__(placeholder="Which player?", options=options)

    async def callback(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.invoker_id:
            await interaction.response.send_message("Only the person who ran this command can choose.", ephemeral=True)
            return
        self.view.stop()
        await self.on_pick(interaction, self.people[self.values[0]])


def _option(p: Person) -> dict:
    """Labelled by whichever name was typed; handles tell SC2 twins apart."""
    if p.discord_name is not None:
        played = f"plays as {p.sc2_name} · {p.games} games" if p.handles else "no SC2 account linked"
        return {"label": p.discord_name[:100], "description": played[:100]}
    if p.handles:
        tag = "linked" if p.discord_id else "unlinked"
        return {"label": p.sc2_name[:100], "description": f"{p.games} games · {tag} · …{p.handles[-1][-6:]}"}
    return {"label": p.sc2_name[:100], "description": f"{p.games} games"}
