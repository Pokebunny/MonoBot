"""Ratings and stats commands, derived from the stored match history."""

import logging

from discord.ext import commands
from services import match_embeds
from services.rating import MIN_DURATION_SECONDS, MIN_WINNER_CONFIDENCE, RatingCache
from services.storage import MatchStore

logger = logging.getLogger(__name__)

# No minimum by default: the conservative rating (mu - 3*sigma) already sinks
# low-game players, and the embed only shows the top 20. Pass !leaderboard <N>
# to filter to players with at least N games.
DEFAULT_MIN_GAMES = 1


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
        await ctx.send(embed=match_embeds.leaderboard(board, min_games))

    @commands.hybrid_command(help="show a player's rating and record")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def rank(self, ctx, *, player: str):
        book = self.ratings.book()
        # Names aren't unique; resolve to the account with the most games.
        candidates = book.by_name(player)
        if not candidates:
            await ctx.send(f"No rated games found for **{player}**.")
            return
        rating = candidates[0]
        board = book.leaderboard(min_games=1)
        rank = next(i for i, r in enumerate(board, 1) if r.handle == rating.handle)
        await ctx.send(embed=match_embeds.player_rank(rating, rank, len(board)))
        if len(candidates) > 1:
            await ctx.send(
                f"*(Note: {len(candidates)} different accounts have played as **{player}**; showing the most active.)*"
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
