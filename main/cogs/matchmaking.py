"""Matchmaking queue: players join, and when the queue fills the bot splits
them into the two most balanced teams using their skill ratings.

The queue is a single in-memory roster (one queue for the bot). discord.py
runs interaction callbacks on one event-loop thread, so no locking is needed.
"""

import logging

import discord
from checks import is_bot_admin
from discord.ext import commands
from models.matchmaking import QueuedPlayer
from resources.config import CONFIG
from services import match_embeds
from services.matchmaking import balance_teams
from services.rating import DEFAULT_MU, DEFAULT_SIGMA, RatingCache
from services.storage import MatchStore

logger = logging.getLogger(__name__)

QUEUE_TARGET = 8  # 4v4


class QueueView(discord.ui.View):
    """Join/Leave buttons attached to the queue message. Persistent (fixed
    custom_ids + registered via client.add_view on cog load), so the buttons
    keep working across bot restarts."""

    def __init__(self, cog: "Matchmaking"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="monobot:queue:join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_join(interaction)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, custom_id="monobot:queue:leave")
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
        self.queue_message: discord.Message | None = None  # the live queue embed

    async def cog_load(self):
        # Register the persistent view so Join/Leave buttons on queue messages
        # from before the last restart still dispatch here.
        self.client.add_view(QueueView(self))

    # -- rating lookup ---------------------------------------------------

    def _queued_player(self, user: discord.abc.User) -> QueuedPlayer:
        """Build a QueuedPlayer, rated by the user's bound SC2 account with the
        most games. Users who are linked but haven't played yet (no bound
        handle) get the new-player default rating."""
        book = self.ratings.book()
        best = None
        for handle in self.store.handles_for(str(user.id)):
            rating = book.rating_for(handle)  # follows account merges
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

    async def _refresh_message(self):
        """Update the tracked queue message after a command changes the queue."""
        if self.queue_message is not None:
            try:
                await self.queue_message.edit(embed=self._status_embed(), view=QueueView(self))
            except discord.HTTPException:
                self.queue_message = None

    # -- commands & interactions -----------------------------------------

    @commands.hybrid_command(help="open the matchmaking queue and ping the community")
    @commands.cooldown(1, 30, commands.BucketType.channel)
    async def queue(self, ctx):
        content = None
        if CONFIG.queue_ping_role_id is not None:
            content = f"<@&{CONFIG.queue_ping_role_id}> a monobattle queue is forming — click **Join** to get in!"
        self.queue_message = await ctx.send(
            content=content,
            embed=self._status_embed(),
            view=QueueView(self),
            allowed_mentions=discord.AllowedMentions(roles=True),
        )

    @commands.hybrid_command(help="remove a player from the queue (e.g. a no-show)")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def bump(self, ctx, member: discord.Member):
        if self.queue.pop(str(member.id), None) is None:
            await ctx.send(f"{member.display_name} isn't in the queue.")
            return
        await self._refresh_message()
        await ctx.send(f"Removed **{member.display_name}** from the queue.")

    @commands.hybrid_command(help="clear the matchmaking queue (mods)")
    @is_bot_admin()
    async def clearqueue(self, ctx):
        self.queue.clear()
        await self._refresh_message()
        await ctx.send("Queue cleared.")

    async def handle_join(self, interaction: discord.Interaction):
        # Re-adopt the message the button lives on (after a restart the
        # tracked reference is gone, but the buttons still work).
        self.queue_message = interaction.message
        uid = str(interaction.user.id)
        if not self.store.sc2_names_for(uid):
            await interaction.response.send_message(
                "You need to link your SC2 name before you can queue. Run `!link <your SC2 name>` first.",
                ephemeral=True,
            )
            return
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
        # Reset the queue message, then announce the teams and ping everyone in.
        await interaction.response.edit_message(embed=self._status_embed(), view=QueueView(self))
        mentions = " ".join(f"<@{p.discord_id}>" for p in players)
        await interaction.followup.send(
            content=f"{mentions} — your match is ready!",
            embed=match_embeds.proposed_match(match),
            allowed_mentions=discord.AllowedMentions(users=True),
        )


async def setup(client):
    await client.add_cog(Matchmaking(client))
