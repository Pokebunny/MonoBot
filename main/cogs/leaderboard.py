"""Ratings and stats commands, derived from the stored match history."""

import logging

from discord.ext import commands
from services import match_embeds
from services.rating import MIN_DURATION_SECONDS, MIN_WINNER_CONFIDENCE, RatingBook
from services.storage import MatchStore

logger = logging.getLogger(__name__)

DEFAULT_MIN_GAMES = 10


class Leaderboard(commands.Cog):
    def __init__(self, client):
        self.client = client
        if not hasattr(client, "match_store"):
            client.match_store = MatchStore()
        self.store: MatchStore = client.match_store
        self._book: RatingBook | None = None
        self._book_version = -1

    def _ratings(self) -> RatingBook:
        """Rating book derived from stored matches, cached until the store
        changes. Rebuilding replays full history (~1s per 1000 matches)."""
        if self._book is None or self._book_version != self.store.change_count:
            self._book = RatingBook.from_matches(m for _, m in self.store.all_matches())
            self._book_version = self.store.change_count
        return self._book

    @commands.hybrid_command(help="show the rating leaderboard")
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def leaderboard(self, ctx, min_games: int = DEFAULT_MIN_GAMES):
        board = self._ratings().leaderboard(min_games=min_games)
        await ctx.send(embed=match_embeds.leaderboard(board, min_games))

    @commands.hybrid_command(help="show a player's rating and record")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def rank(self, ctx, *, player: str):
        book = self._ratings()
        rating = book.ratings.get(player)
        if rating is None:
            # Case-insensitive fallback so !rank pokebunny works.
            matches = [r for name, r in book.ratings.items() if name.lower() == player.lower()]
            rating = matches[0] if matches else None
        if rating is None or rating.games == 0:
            await ctx.send(f"No rated games found for **{player}**.")
            return
        board = book.leaderboard(min_games=1)
        rank = next(i for i, r in enumerate(board, 1) if r.name == rating.name)
        await ctx.send(embed=match_embeds.player_rank(rating, rank, len(board)))

    @commands.hybrid_command(help="show win rates by unit pick")
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def unitstats(self, ctx, min_games: int = 10):
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
