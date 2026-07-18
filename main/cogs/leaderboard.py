"""Ratings and stats commands, derived from the stored match history."""

import logging

import discord
from discord.ext import commands
from services import match_embeds
from services.rating import MIN_DURATION_SECONDS, MIN_WINNER_CONFIDENCE, RatingCache
from services.storage import MatchStore
from views import ExpiringView

logger = logging.getLogger(__name__)

# No minimum by default: the conservative rating already sinks low-game players
# and the board paginates. Pass !leaderboard <N> to require at least N games.
DEFAULT_MIN_GAMES = 1


class LeaderboardView(ExpiringView):
    """◀ ▶ pagination for the leaderboard. Snapshots the ranking so paging
    stays consistent even if a game is uploaded mid-browse."""

    def __init__(self, board, min_games: int):
        super().__init__()
        self.board = board
        self.min_games = min_games
        self.page = 0
        self.pages = match_embeds.leaderboard_page_count(board)
        self._sync()

    @property
    def multipage(self) -> bool:
        return self.pages > 1

    def _sync(self):
        at_start = self.page <= 0
        at_end = self.page >= self.pages - 1
        self.first.disabled = self.prev.disabled = at_start
        self.next.disabled = self.last.disabled = at_end

    async def _show(self, interaction: discord.Interaction):
        self._sync()
        await interaction.response.edit_message(
            embed=match_embeds.leaderboard(self.board, self.page, self.min_games), view=self
        )

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


class Leaderboard(commands.Cog):
    def __init__(self, client):
        self.client = client
        if not hasattr(client, "match_store"):
            client.match_store = MatchStore()
        if not hasattr(client, "rating_cache"):
            client.rating_cache = RatingCache(client.match_store)
        self.store: MatchStore = client.match_store
        self.ratings: RatingCache = client.rating_cache

    @commands.hybrid_command(help="show the rating leaderboard")
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def leaderboard(self, ctx, min_games: int = DEFAULT_MIN_GAMES):
        board = self.ratings.book().leaderboard(min_games=min_games)
        view = LeaderboardView(board, min_games)
        message = await ctx.send(
            embed=match_embeds.leaderboard(board, 0, min_games), view=view if view.multipage else None
        )
        view.message = message

    def _resolve(self, player: str):
        """Resolve a display name (current or former) to (rating, rank, board
        size, number of same-named accounts), or None if no rated games.
        Names aren't unique, so pick the most-active matching account."""
        book = self.ratings.book()
        rated, seen = [], set()
        for h in self.store.handles_for_name(player):
            r = book.rating_for(h)  # follows account merges
            if r is not None and r.handle not in seen:
                seen.add(r.handle)
                rated.append(r)
        if not rated:
            return None
        rated.sort(key=lambda r: r.games, reverse=True)
        rating = rated[0]
        board = book.leaderboard(min_games=1)
        rank = next(i for i, r in enumerate(board, 1) if r.handle == rating.handle)
        return rating, rank, len(board), len(rated)

    def _resolve_self(self, author):
        """The command author's own most-active rated account, or None."""
        book = self.ratings.book()
        best = None
        for h in self.store.handles_for(str(author.id)):
            r = book.rating_for(h)
            if r is not None and (best is None or r.games > best.games):
                best = r
        if best is None:
            return None
        board = book.leaderboard(min_games=1)
        rank = next((i for i, r in enumerate(board, 1) if r.handle == best.handle), len(board))
        return best, rank, len(board), 1

    async def _resolve_or_reply(self, ctx, player: str | None):
        if player is None:
            resolved = self._resolve_self(ctx.author)
            if resolved is None:
                await ctx.send("You haven't linked a rated SC2 account yet — use `!link <name>`, or pass a name.")
            return resolved
        resolved = self._resolve(player)
        if resolved is None:
            await ctx.send(f"No rated games found for **{player}**.")
        return resolved

    @commands.hybrid_command(help="show a player's rating and record (yourself if no name given)")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def rank(self, ctx, *, player: str | None = None):
        resolved = await self._resolve_or_reply(ctx, player)
        if resolved is None:
            return
        rating, rank, total, n_accounts = resolved
        aliases = self.store.aliases_for_handle(rating.handle)
        await ctx.send(embed=match_embeds.player_rank(rating, rank, total, aliases))
        if n_accounts > 1:
            await ctx.send(
                f"*(Note: {n_accounts} different accounts have played as **{player}**; showing the most active.)*"
            )

    @commands.hybrid_command(help="show a player's full profile (yourself if no name given)")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def profile(self, ctx, *, player: str | None = None):
        resolved = await self._resolve_or_reply(ctx, player)
        if resolved is None:
            return
        rating, rank, total, n_accounts = resolved
        aliases = self.store.aliases_for_handle(rating.handle)
        races = self.store.player_records_by(rating.handle, "race", MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS)
        units = self.store.player_records_by(rating.handle, "pick", MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS)
        await ctx.send(embed=match_embeds.player_profile(rating, rank, total, aliases, races, units))
        if n_accounts > 1:
            await ctx.send(
                f"*(Note: {n_accounts} different accounts have played as **{player}**; showing the most active.)*"
            )

    @commands.hybrid_command(help="show win rates by unit pick")
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def unitstats(self, ctx, min_games: int = 1):
        records = self.store.unit_records(MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS)
        if not records:
            await ctx.send("No decided matches stored yet.")
            return
        await ctx.send(embed=match_embeds.unit_stats(records, min_games))

    @commands.hybrid_command(help="how many matches are stored")
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def matchcount(self, ctx):
        await ctx.send(f"{self.store.match_count()} matches stored.")


async def setup(client):
    await client.add_cog(Leaderboard(client))
