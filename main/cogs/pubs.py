"""Stats over every monobattle on record: the pub (non-community) archive
plus every community's ladder, each game counted once. Ladder games never
leave their community's ratings, but pooled here their volume makes unit win
rates far more reliable than any one ladder. The pub archive is populated by
scripts/split_pubs.py."""

import logging
import os

from discord.ext import commands
from services import match_embeds
from services.rating import MIN_DURATION_SECONDS, MIN_WINNER_CONFIDENCE
from services.storage import MatchStore, combined_game_count, combined_unit_records

logger = logging.getLogger(__name__)

PUBS_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "resources", "pubs.db")


class Pubs(commands.Cog):
    def __init__(self, client):
        self.client = client
        # A separate store/DB from the community ladders — pub games don't
        # move anyone's ratings, but their volume gives better aggregate stats.
        self.store = MatchStore(PUBS_DB_PATH)

    def _stores(self) -> list[MatchStore]:
        """Every store, communities first: pubs.db is a one-off snapshot that
        also holds the home ladder's older games, so a game both have is read
        from the ladder, where a later winner confirmation lands."""
        return [c.store for c in self.client.communities.all()] + [self.store]

    @commands.hybrid_command(help="unit win rates across every game on record — pubs and all community ladders")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def pubunitstats(self, ctx, min_games: int = 10):
        records = combined_unit_records(self._stores(), MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS)
        if not records:
            await ctx.send("No decided games on record yet.")
            return
        embed = match_embeds.unit_stats(records, min_games)
        embed.title = "Pub Unit Win Rates"
        await ctx.send(embed=embed)

    @commands.hybrid_command(help="how many games are on record across pubs and all community ladders")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def pubcount(self, ctx):
        await ctx.send(f"{combined_game_count(self._stores())} games on record across pubs and every community.")


async def setup(client):
    await client.add_cog(Pubs(client))
