"""Matchmaking queue: players join, and when the queue fills the bot splits
them into the two most balanced teams using their skill ratings.

The queue is a single in-memory roster (one queue for the bot). discord.py
runs interaction callbacks on one event-loop thread, so no locking is needed.
"""

import logging

import discord
from discord.ext import commands
from models.matchmaking import QueuedPlayer
from services import match_embeds
from services.matchmaking import balance_teams
from services.rating import DEFAULT_MU, DEFAULT_SIGMA, RatingCache
from services.storage import MatchStore

logger = logging.getLogger(__name__)

QUEUE_TARGET = 8  # 4v4


class QueueView(discord.ui.View):
    """Join/Leave buttons attached to the queue message."""

    def __init__(self, cog: "Matchmaking"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_join(interaction)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_leave(interaction)


class Matchmaking(commands.Cog):
    def __init__(self, client):
        self.client = client
        if not hasattr(client, "match_store"):
            client.match_store = MatchStore()
        if not hasattr(client, "rating_cache"):
            client.rating_cache = RatingCache(client.match_store)
        self.store: MatchStore = client.match_store
        self.ratings: RatingCache = client.rating_cache
        self.queue: dict[str, discord.abc.User] = {}

    # -- rating lookup ---------------------------------------------------

    def _queued_player(self, user: discord.abc.User) -> QueuedPlayer:
        """Build a QueuedPlayer, rating the user by their linked SC2 name with
        the most games. Unlinked users get the new-player default rating."""
        book = self.ratings.book()
        best = None
        for name in self.store.sc2_names_for(str(user.id)):
            rating = book.ratings.get(name)
            if rating is not None and (best is None or rating.games > best.games):
                best = rating
        if best is not None:
            return QueuedPlayer(
                discord_id=str(user.id),
                display_name=user.display_name,
                sc2_name=best.name,
                mu=best.mu,
                sigma=best.sigma,
            )
        return QueuedPlayer(
            discord_id=str(user.id),
            display_name=user.display_name,
            sc2_name=None,
            mu=DEFAULT_MU,
            sigma=DEFAULT_SIGMA,
        )

    def _players(self) -> list[QueuedPlayer]:
        return [self._queued_player(u) for u in self.queue.values()]

    def _status_embed(self) -> discord.Embed:
        return match_embeds.queue_status(self._players(), QUEUE_TARGET)

    # -- commands & interactions -----------------------------------------

    @commands.hybrid_command(help="open the matchmaking queue")
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def queue(self, ctx):
        await ctx.send(embed=self._status_embed(), view=QueueView(self))

    async def handle_join(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        if uid in self.queue:
            await interaction.response.send_message("You're already in the queue.", ephemeral=True)
            return
        self.queue[uid] = interaction.user
        if len(self.queue) >= QUEUE_TARGET:
            await self._form_match(interaction)
        else:
            await interaction.response.edit_message(embed=self._status_embed(), view=QueueView(self))

    async def handle_leave(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        if self.queue.pop(uid, None) is None:
            await interaction.response.send_message("You're not in the queue.", ephemeral=True)
            return
        await interaction.response.edit_message(embed=self._status_embed(), view=QueueView(self))

    async def _form_match(self, interaction: discord.Interaction):
        players = self._players()[:QUEUE_TARGET]
        match = balance_teams(players)
        self.queue.clear()
        # Reset the queue message, then announce the teams.
        await interaction.response.edit_message(embed=self._status_embed(), view=QueueView(self))
        await interaction.followup.send(embed=match_embeds.proposed_match(match))


async def setup(client):
    await client.add_cog(Matchmaking(client))
