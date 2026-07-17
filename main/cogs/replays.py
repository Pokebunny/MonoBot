"""Replay ingestion: watch for .SC2Replay attachments, parse, store, and
ask for manual winner confirmation when inference is below the rating gate."""

import asyncio
import logging
import os
import tempfile

import discord
from discord.ext import commands
from resources.config import CONFIG
from services import match_embeds, replay_parser, storage
from services.rating import MIN_DURATION_SECONDS, MIN_WINNER_CONFIDENCE
from services.storage import MatchStore

logger = logging.getLogger(__name__)


class ConfirmWinnerView(discord.ui.View):
    """Two buttons to settle a match whose winner couldn't be inferred
    confidently. Only a player who was in the match may confirm it — checked
    via their linked SC2 name(s). Manual confirmation sets confidence to 1.0."""

    def __init__(self, store: MatchStore, match_id: int):
        super().__init__(timeout=None)
        self.store = store
        self.match_id = match_id

    def _is_participant(self, discord_id: str) -> bool:
        handles = set(self.store.handles_for(discord_id))
        if not handles:
            return False
        match = self.store.get_match(self.match_id)
        if match is None:
            return False
        return any(p.toon_handle in handles for p in match.players)

    async def _confirm(self, interaction: discord.Interaction, team: int):
        if not self._is_participant(str(interaction.user.id)):
            await interaction.response.send_message(
                "Only a player who was in this match can confirm it. "
                "Link your SC2 name first with `!link <your SC2 name>`.",
                ephemeral=True,
            )
            return
        self.store.confirm_winner(self.match_id, team)
        match = self.store.get_match(self.match_id)
        embed = match_embeds.match_summary(match, self.match_id)
        embed.set_footer(text=f"Match #{self.match_id} · confirmed by {interaction.user.display_name}")
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()

    @discord.ui.button(label="Team 1 won", style=discord.ButtonStyle.primary)
    async def team1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._confirm(interaction, 1)

    @discord.ui.button(label="Team 2 won", style=discord.ButtonStyle.secondary)
    async def team2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._confirm(interaction, 2)


class Replays(commands.Cog):
    def __init__(self, client):
        self.client = client
        if not hasattr(client, "match_store"):
            client.match_store = MatchStore()
        self.store: MatchStore = client.match_store

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        # Only watch the configured replays channel (None = every channel).
        if CONFIG.replays_channel_id is not None and message.channel.id != CONFIG.replays_channel_id:
            return
        for attachment in message.attachments:
            if attachment.filename.lower().endswith(".sc2replay"):
                await self._process_attachment(message.channel, attachment, str(message.author))

    async def _process_attachment(self, channel, attachment: discord.Attachment, uploader: str):
        data = await attachment.read()
        file_hash = storage.hash_replay(data)
        if self.store.has_replay(file_hash):
            await channel.send(embed=match_embeds.duplicate_notice(attachment.filename))
            return

        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, attachment.filename)
                with open(path, "wb") as f:
                    f.write(data)
                # sc2reader parsing takes ~1-2s; keep the event loop free.
                match = await asyncio.to_thread(replay_parser.parse_replay, path)
        except Exception as e:
            logger.exception("Failed to parse %s", attachment.filename)
            await channel.send(embed=match_embeds.parse_failure(attachment.filename, str(e)))
            return

        result = self.store.ingest(match, file_hash, uploaded_by=uploader)
        if result.status == "duplicate":
            # Same game already stored (this file, or a recording at least as
            # complete from another player).
            await channel.send(embed=match_embeds.duplicate_notice(attachment.filename))
            return

        embed = match_embeds.match_summary(match, result.match_id)
        if result.status == "updated":
            embed.set_footer(text=f"Match #{result.match_id} · refined from a more complete recording")
        needs_confirmation = match.duration_seconds >= MIN_DURATION_SECONDS and (
            match.winning_team is None or match.winner_confidence < MIN_WINNER_CONFIDENCE
        )
        if needs_confirmation:
            await channel.send(embed=embed, view=ConfirmWinnerView(self.store, result.match_id))
        else:
            await channel.send(embed=embed)

    @commands.hybrid_command(help="list stored matches that still need a winner confirmed")
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def pending(self, ctx):
        pending = self.store.pending_confirmations(MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS)
        if not pending:
            await ctx.send("No matches waiting on confirmation.")
            return
        # Re-post the most recent few with confirm buttons.
        for match_id, match in pending[-3:]:
            await ctx.send(
                embed=match_embeds.match_summary(match, match_id),
                view=ConfirmWinnerView(self.store, match_id),
            )
        if len(pending) > 3:
            await ctx.send(f"...and {len(pending) - 3} more. Confirm these and run !pending again.")


async def setup(client):
    await client.add_cog(Replays(client))
