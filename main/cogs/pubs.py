"""Stats over the pub (non-community) game archive, kept in a separate DB from
the community ladder. Populated by scripts/split_pubs.py; the big sample makes
unit win rates far more reliable than the smaller community ladder."""

import logging
import os

from discord.ext import commands
from services import match_embeds
from services.rating import MIN_DURATION_SECONDS, MIN_WINNER_CONFIDENCE
from services.storage import MatchStore

logger = logging.getLogger(__name__)

PUBS_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "resources", "pubs.db")


class Pubs(commands.Cog):
    def __init__(self, client):
        self.client = client
        # A separate store/DB from the community ladder — pub games don't move
        # community ratings, but their volume gives better aggregate stats.
        self.store = MatchStore(PUBS_DB_PATH)

    @commands.hybrid_command(help="unit win rates across the pub (non-community) archive")
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def pubunitstats(self, ctx, min_games: int = 10):
        records = self.store.unit_records(MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS)
        if not records:
            await ctx.send("No pub games archived yet — run `scripts/split_pubs.py`.")
            return
        embed = match_embeds.unit_stats(records, min_games)
        embed.title = "Pub Unit Win Rates"
        await ctx.send(embed=embed)

    @commands.hybrid_command(help="how many pub games are archived")
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def pubcount(self, ctx):
        await ctx.send(f"{self.store.match_count()} pub games archived.")


async def setup(client):
    await client.add_cog(Pubs(client))
