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
        aliases = self.store.aliases_for_handles(self.store.merged_handles(rating.handle))
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
        group = self.store.merged_handles(rating.handle)  # all merged accounts, e.g. Jay+Luigi
        aliases = self.store.aliases_for_handles(group)
        races = self.store.player_records_by(group, "race", MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS)
        units = self.store.player_records_by(group, "pick", MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS)
        mvps = self.store.mvp_count(group, MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS)
        awards = self.store.award_counts(group, MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS)
        await ctx.send(embed=match_embeds.player_profile(rating, rank, total, aliases, races, units, mvps, awards))
        if n_accounts > 1:
            await ctx.send(
                f"*(Note: {n_accounts} different accounts have played as **{player}**; showing the most active.)*"
            )

    def _group_for_name(self, player: str) -> tuple[str, list[str]]:
        """A display name's most-active account and its full merge group."""
        candidates = self.store.candidates_for_name(player)
        if not candidates:
            return player, []
        handle, name, _games = candidates[0]
        return name, self.store.merged_handles(handle)

    def _own_group(self, author) -> list[str]:
        group: list[str] = []
        for handle in self.store.handles_for(str(author.id)):
            for h in self.store.merged_handles(handle):
                if h not in group:
                    group.append(h)
        return group

    @commands.hybrid_command(help="show the most recent match — or a player's; !last <name> [count up to 3]")
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def last(self, ctx, player: str | None = None, count: commands.Range[int, 1, 3] = 1):
        if player and player.isdigit():  # allow `!last 3` without a name
            count, player = min(int(player), 3), None
        matches = self.store.all_matches()  # oldest first
        if player:
            name, group = self._group_for_name(player)
            if not group:
                await ctx.send(f"No games found for **{player}**.")
                return
            handles = set(group)
            matches = [(i, m) for i, m in matches if any(p.toon_handle in handles for p in m.players)]
        if not matches:
            await ctx.send("No matches stored yet.")
            return
        # Oldest of the batch first, so the newest game ends up at the bottom.
        for match_id, match in matches[-count:]:
            await ctx.send(embed=match_embeds.match_summary(match, match_id))

    @commands.hybrid_command(help="head-to-head between two players — !h2h <name> means you vs them")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def h2h(self, ctx, player1: str, player2: str | None = None):
        name1, group1 = self._group_for_name(player1)
        if not group1:
            await ctx.send(f"No games found for **{player1}**.")
            return
        if player2 is None:
            group2 = self._own_group(ctx.author)
            if not group2:
                await ctx.send("Link your SC2 account first (`!link <name>`), or give two names.")
                return
            name2 = ctx.author.display_name
        else:
            name2, group2 = self._group_for_name(player2)
            if not group2:
                await ctx.send(f"No games found for **{player2}**.")
                return
        if set(group1) & set(group2):
            await ctx.send(f"**{name1}** and **{name2}** are the same player.")
            return
        vs, together, opposed = self.store.h2h_records(group1, group2, MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS)
        if not (sum(vs) + sum(together)):
            await ctx.send(f"**{name1}** and **{name2}** haven't shared a decided game yet.")
            return
        await ctx.send(embed=match_embeds.h2h_summary(name1, name2, vs, together, opposed, group1, group2))
        if opposed:
            match_id, match = opposed[-1]  # their most recent meeting, in full
            await ctx.send(embed=match_embeds.match_summary(match, match_id))

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
