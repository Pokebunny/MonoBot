"""Replay ingestion: watch for .SC2Replay attachments, parse, store, and
ask for manual winner confirmation when inference is below the rating gate."""

import asyncio
import logging
import os
import tempfile
from collections import Counter

import discord
from checks import is_bot_admin
from discord.ext import commands
from resources.config import CONFIG
from services import achievements, match_embeds, replay_parser, storage
from services.achievements import AchievementCache
from services.rating import MIN_DURATION_SECONDS, MIN_WINNER_CONFIDENCE
from services.storage import MatchStore
from views import ExpiringView

logger = logging.getLogger(__name__)

# Raw uploads are archived here (gitignored) so future parser improvements
# can re-process history instead of only applying to new games.
REPLAY_ARCHIVE_DIR = os.path.join(os.path.dirname(__file__), "..", "resources", "replays")


def archive_replay(data: bytes, file_hash: str) -> None:
    os.makedirs(REPLAY_ARCHIVE_DIR, exist_ok=True)
    path = os.path.join(REPLAY_ARCHIVE_DIR, f"{file_hash}.SC2Replay")
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)


class ConfirmWinnerView(ExpiringView):
    """Two buttons to settle a match whose winner couldn't be inferred
    confidently. Only a player who was in the match may confirm it — checked
    via their linked SC2 name(s). Manual confirmation sets confidence to 1.0.
    Expires after 24h (a restart kills it anyway); !pending re-posts buttons."""

    def __init__(self, store: MatchStore, match_id: int, achievements: AchievementCache | None = None):
        super().__init__()
        self.store = store
        self.match_id = match_id
        self.achievements = achievements

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
        if self.achievements:
            pre_discovered = self.store.discovered_keys()  # before grant, to spot community-first secrets
            unlocks = achievements.grant_new_unlocks(self.store, self.achievements, match)
            if unlocks:
                first = frozenset(
                    e.spec.key
                    for _, e in unlocks
                    if achievements.is_secret(e.spec) and e.spec.key not in pre_discovered
                )
                await interaction.followup.send(embed=match_embeds.achievement_unlocks(unlocks, first))

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
        if not hasattr(client, "achievement_cache"):
            client.achievement_cache = AchievementCache(client.match_store)
        self.store: MatchStore = client.match_store
        self.achievements: AchievementCache = client.achievement_cache
        # First run over an existing history: grant the career backfill
        # silently so the first upload doesn't announce years of badges.
        achievements.ensure_seeded(self.store, self.achievements)

    def _watched_channels(self) -> set[int]:
        """Channels watched for replays: the runtime !watchreplays set plus
        any config-file channels. Empty = watch everywhere (dev default)."""
        return self.store.replay_channel_ids() | set(CONFIG.replays_channel_ids)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        watched = self._watched_channels()
        if watched and message.channel.id not in watched:
            return
        for attachment in message.attachments:
            if attachment.filename.lower().endswith(".sc2replay"):
                await self._process_attachment(message.channel, attachment, message.author)

    @commands.hybrid_command(help="toggle watching this channel (or #channel) for replay uploads (mods)")
    @is_bot_admin()
    async def watchreplays(self, ctx, channel: discord.TextChannel | None = None):
        target = channel or ctx.channel
        if target.id in set(CONFIG.replays_channel_ids) - self.store.replay_channel_ids():
            await ctx.send(f"{target.mention} is watched via the config file — remove it there to stop.")
            return
        guild_id = target.guild.id if target.guild else None
        now_watched = self.store.toggle_replay_channel(target.id, guild_id, str(ctx.author.id))
        verb = "Now watching" if now_watched else "Stopped watching"
        mentions = ", ".join(f"<#{cid}>" for cid in sorted(self._watched_channels())) or "every channel"
        await ctx.send(f"{verb} {target.mention} for replay uploads. Currently watching: {mentions}")

    async def _process_attachment(self, channel, attachment: discord.Attachment, author: discord.abc.User):
        # uploaded_by keys on the Discord id so Chronicler can credit it.
        uploader = str(author.id)
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

        archive_replay(data, file_hash)
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
            view = ConfirmWinnerView(self.store, result.match_id, self.achievements)
            view.message = await channel.send(embed=embed, view=view)
        else:
            await channel.send(embed=embed)
        pre_discovered = self.store.discovered_keys()  # before grant, to spot community-first secrets
        unlocks = achievements.grant_new_unlocks(self.store, self.achievements, match)
        unlocks += self._chronicler_unlock(author, match)
        if unlocks:
            first = frozenset(
                e.spec.key for _, e in unlocks if achievements.is_secret(e.spec) and e.spec.key not in pre_discovered
            )
            await channel.send(embed=match_embeds.achievement_unlocks(unlocks, first))

    def _chronicler_unlock(self, author, match) -> list:
        """Chronicler is earned by uploading, not playing — grant it here
        when this upload crosses the threshold (needs a linked account to
        hang the badge on)."""
        if self.store.upload_count(str(author.id)) < achievements.CHRONICLER_UPLOADS:
            return []
        handles = self.store.handles_for(str(author.id))
        if not handles:
            return []
        spec = achievements.SPECS_BY_KEY["chronicler"]
        if achievements.grant_direct(self.store, handles[0], spec.key, match.played_at):
            return [(author.display_name, achievements.Earned(spec, match.played_at))]
        return []

    @commands.hybrid_command(help="re-scan a channel's history for replays, refreshing stored games (mods)")
    @is_bot_admin()
    async def backfillchannel(self, ctx, channel: discord.TextChannel | None = None, limit: int = 2000):
        """Downloads every .SC2Replay ever posted in the channel, archives the
        raw files, re-parses already-stored games in place (so new parser
        fields apply retroactively), and ingests any games we missed."""
        target = channel or ctx.channel
        await ctx.send(f"Scanning the last {limit} messages of {target.mention} for replays…")
        stats = Counter()
        async for message in target.history(limit=limit, oldest_first=True):
            for attachment in message.attachments:
                if not attachment.filename.lower().endswith(".sc2replay"):
                    continue
                try:
                    data = await attachment.read()
                except discord.HTTPException:
                    stats["download failed"] += 1
                    continue
                file_hash = storage.hash_replay(data)
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        path = os.path.join(tmp, attachment.filename)
                        with open(path, "wb") as f:
                            f.write(data)
                        match = await asyncio.to_thread(replay_parser.parse_replay, path)
                except Exception:
                    logger.exception("Backfill: failed to parse %s", attachment.filename)
                    stats["parse failed"] += 1
                    continue
                archive_replay(data, file_hash)
                if self.store.refresh_parse(match, file_hash):
                    stats["refreshed"] += 1
                else:
                    result = self.store.ingest(match, file_hash, uploaded_by=str(message.author.id))
                    stats[result.status] += 1
        granted = achievements.sweep_grants(self.store, self.achievements)
        if granted:
            stats["achievements granted quietly"] = granted
        summary = ", ".join(f"{k}: {n}" for k, n in stats.most_common()) or "no replays found"
        await ctx.send(f"Backfill of {target.mention} done — {summary}.")

    @commands.hybrid_command(help="list stored matches that still need a winner confirmed")
    @commands.cooldown(1, 5, commands.BucketType.channel)
    async def pending(self, ctx):
        pending = self.store.pending_confirmations(MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS)
        if not pending:
            await ctx.send("No matches waiting on confirmation.")
            return
        # Re-post the most recent few with confirm buttons.
        for match_id, match in pending[-3:]:
            view = ConfirmWinnerView(self.store, match_id, self.achievements)
            view.message = await ctx.send(embed=match_embeds.match_summary(match, match_id), view=view)
        if len(pending) > 3:
            await ctx.send(f"...and {len(pending) - 3} more. Confirm these and run !pending again.")


async def setup(client):
    await client.add_cog(Replays(client))
