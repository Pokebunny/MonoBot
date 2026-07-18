"""Link Discord users to the SC2 name(s) they play under.

Winner confirmation (in the replays cog) is restricted to players who were in
the match, and that check runs through these links: a member can only confirm
a game if one of their linked SC2 names is in it.
"""

import logging

import discord
from checks import is_bot_admin
from discord.ext import commands
from services.match_embeds import ACCENT, WARNING
from services.storage import MatchStore
from views import ExpiringView

logger = logging.getLogger(__name__)


class _AccountSelect(discord.ui.Select):
    """Dropdown to pick which account a shared name belongs to. In add_mode it
    attaches an ADDITIONAL account (multi-account player). operator_id is who
    may click — the person themself, or the admin running !linkuser."""

    def __init__(
        self,
        store: MatchStore,
        discord_id: str,
        sc2_name: str,
        candidates,
        add_mode: bool = False,
        operator_id: str | None = None,
        target_label: str = "your account",
    ):
        self.store = store
        self.discord_id = discord_id
        self.sc2_name = sc2_name
        self.add_mode = add_mode
        self.operator_id = operator_id or discord_id
        self.target_label = target_label
        options = [
            discord.SelectOption(label=name[:100], description=f"{games} games · …{handle[-6:]}", value=handle)
            for handle, name, games in candidates[:25]
        ]
        super().__init__(placeholder="Which account?", options=options)

    async def callback(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.operator_id:
            await interaction.response.send_message("Only the person who ran this command can choose.", ephemeral=True)
            return
        if self.add_mode:
            ok = self.store.add_account(self.discord_id, self.values[0])
        else:
            ok = self.store.bind_specific(self.discord_id, self.sc2_name, self.values[0])
        if ok:
            msg = f"Linked **{self.sc2_name}** to {self.target_label} — their games count toward one rating."
        else:
            msg = "That account is already linked to someone else — use `!unlinkuser` first if that's wrong."
        await interaction.response.edit_message(content=msg, embed=None, view=None)
        self.view.stop()


class DisambiguationView(ExpiringView):
    def __init__(
        self,
        store: MatchStore,
        discord_id: str,
        sc2_name: str,
        candidates,
        add_mode: bool = False,
        operator_id: str | None = None,
        target_label: str = "your account",
    ):
        super().__init__()
        self.add_item(_AccountSelect(store, discord_id, sc2_name, candidates, add_mode, operator_id, target_label))


class ConfirmAddView(ExpiringView):
    """Shown when someone links a name while already having an account linked —
    confirms they mean to add a second (merged) account, not replace."""

    def __init__(self, cog: "Identity", discord_id: str, sc2_name: str):
        super().__init__()
        self.cog = cog
        self.discord_id = discord_id
        self.sc2_name = sc2_name

    async def _mine(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.discord_id:
            await interaction.response.send_message("Only you can confirm your own link.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Add account", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._mine(interaction):
            await self.cog.resolve_additional(interaction, self.discord_id, self.sc2_name)
            self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._mine(interaction):
            await interaction.response.edit_message(content="Cancelled — no account added.", embed=None, view=None)
            self.stop()


class Identity(commands.Cog):
    def __init__(self, client):
        self.client = client
        if not hasattr(client, "match_store"):
            client.match_store = MatchStore()
        self.store: MatchStore = client.match_store

    def _account_summary(self, discord_id: str) -> str:
        parts = []
        for h in self.store.handles_for(discord_id):
            aliases = self.store.aliases_for_handle(h)
            parts.append(aliases[0] if aliases else h)
        parts += self.store.pending_names_for(discord_id)
        return ", ".join(f"**{p}**" for p in parts) or "an account"

    async def resolve_additional(self, interaction: discord.Interaction, discord_id: str, sc2_name: str):
        """Add a second account after the user confirms. Direct if the name is
        unambiguous, otherwise show the account picker."""
        candidates = self.store.candidates_for_name(sc2_name)
        if not candidates:
            result = self.store.link_player(discord_id, sc2_name)
            content = (
                f"**{sc2_name}** is linked to someone else."
                if result.status == "taken"
                else f"Added **{sc2_name}** — it'll bind to your account the first time it's seen in a game."
            )
            await interaction.response.edit_message(content=content, embed=None, view=None)
        elif len(candidates) == 1:
            ok = self.store.add_account(discord_id, candidates[0][0])
            content = (
                "Added that account — all your accounts now share one rating."
                if ok
                else "That account is already linked to someone else."
            )
            await interaction.response.edit_message(content=content, embed=None, view=None)
        else:
            embed = discord.Embed(
                title="Add which account?",
                description=f"**{sc2_name}** matches {len(candidates)} accounts — pick the one to add.",
                color=ACCENT,
            )
            view = DisambiguationView(self.store, discord_id, sc2_name, candidates, add_mode=True)
            view.message = interaction.message
            await interaction.response.edit_message(embed=embed, view=view)

    @commands.hybrid_command(help="link your Discord account to your in-game SC2 name")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def link(self, ctx, *, sc2_name: str):
        sc2_name = sc2_name.strip()
        if not sc2_name:
            await ctx.send("Usage: `!link <your SC2 name>`")
            return

        discord_id = str(ctx.author.id)
        # Already have an account? Adding another is a merge — confirm first.
        if self.store.sc2_names_for(discord_id):
            embed = discord.Embed(
                title="Add another account?",
                description=f"You already have {self._account_summary(discord_id)} linked. "
                f"Linking **{sc2_name}** adds it as another of *your* accounts — they'll share one "
                "combined rating. Continue?",
                color=WARNING,
            )
            view = ConfirmAddView(self, discord_id, sc2_name)
            view.message = await ctx.send(embed=embed, view=view)
            return

        result = self.store.link_player(discord_id, sc2_name)
        if result.status == "taken":
            embed = discord.Embed(
                title="Name already claimed",
                description=f"**{sc2_name}** is already linked to another Discord user. "
                "If that's wrong, an admin can help sort it out.",
                color=WARNING,
            )
            await ctx.send(embed=embed)
            return

        if result.status == "ambiguous":
            candidates = self.store.candidates_for_name(sc2_name)
            embed = discord.Embed(
                title="Which account is yours?",
                description=f"**{result.candidates} different accounts** have played as **{sc2_name}**. "
                "Pick yours from the menu below (by game count / account id).",
                color=WARNING,
            )
            view = DisambiguationView(self.store, str(ctx.author.id), sc2_name, candidates)
            view.message = await ctx.send(embed=embed, view=view)
            return

        embed = discord.Embed(
            title="Linked",
            description=f"You're now linked to **{sc2_name}**.",
            color=ACCENT,
        )
        if result.handle is None:
            embed.add_field(
                name="Heads up",
                value="That name hasn't appeared in any stored match yet — double-check the spelling "
                "(it must match your in-game name exactly). It'll bind to your account the first time "
                "you play.",
                inline=False,
            )
        names = self.store.sc2_names_for(str(ctx.author.id))
        if len(names) > 1:
            embed.set_footer(text="Your names: " + ", ".join(names))
        await ctx.send(embed=embed)

    @commands.hybrid_command(help="link a member to an SC2 account (mods) — repeat to attach a player's alt accounts")
    @is_bot_admin()
    async def linkuser(self, ctx, member: discord.Member, *, sc2_name: str):
        sc2_name = sc2_name.strip()
        discord_id = str(member.id)
        candidates = self.store.candidates_for_name(sc2_name)
        if not candidates:
            result = self.store.link_player(discord_id, sc2_name)
            if result.status == "taken":
                await ctx.send(
                    f"**{sc2_name}** is already claimed by <@{result.owner}> — `!unlinkuser` it first.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await ctx.send(f"Linked **{member.display_name}** to **{sc2_name}** — it'll bind on their next game.")
            return

        # A claim on this name that can't simply be bound (already bound, or
        # held by someone else) means the account attaches by handle instead.
        claim_owner = self.store.discord_id_for(sc2_name)
        claim_pending = claim_owner is not None and any(
            n.lower() == sc2_name.lower() for n in self.store.pending_names_for(claim_owner)
        )
        add_mode = claim_owner is not None and not (claim_owner == discord_id and claim_pending)

        if len(candidates) > 1:
            embed = discord.Embed(
                title="Which account?",
                description=f"**{sc2_name}** matches {len(candidates)} accounts — pick the one to link to "
                f"**{member.display_name}** (by game count / account id).",
                color=WARNING,
            )
            view = DisambiguationView(
                self.store,
                discord_id,
                sc2_name,
                candidates,
                add_mode=add_mode,
                operator_id=str(ctx.author.id),
                target_label=f"**{member.display_name}**",
            )
            view.message = await ctx.send(embed=embed, view=view)
            return

        handle = candidates[0][0]
        owner = self.store.discord_id_for_handle(handle)
        if owner == discord_id:
            await ctx.send(f"{member.display_name} already has **{sc2_name}** linked.")
            return
        if owner is not None:
            await ctx.send(
                f"Refusing: that account is linked to <@{owner}> — two different people can't share an "
                "account. `!unlinkuser` it first if the old link is wrong.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        ok = (
            self.store.add_account(discord_id, handle)
            if add_mode
            else self.store.bind_specific(discord_id, sc2_name, handle)
        )
        if ok:
            await ctx.send(f"Linked **{member.display_name}** to **{sc2_name}** — their games count toward one rating.")
        else:
            await ctx.send(f"Couldn't link **{sc2_name}** — its claim conflicts; try `!unlinkuser` first.")

    @commands.hybrid_command(help="unlink a member's SC2 accounts, or one name — @member [name], or a bare name (mods)")
    @is_bot_admin()
    async def unlinkuser(self, ctx, target: str, *, sc2_name: str | None = None):
        try:
            member = await commands.MemberConverter().convert(ctx, target)
        except commands.BadArgument:
            member = None
        if member is None:
            # A bare SC2 name — release the claim whoever holds it (covers
            # people who've left the server).
            if sc2_name:
                await ctx.send("Give either `@member [name]` or just an SC2 name.")
                return
            owner = self.store.release_name(target)
            if owner is None:
                await ctx.send(f"**{target}** isn't linked to anyone.")
            else:
                await ctx.send(f"Unlinked **{target}** (was linked to <@{owner}>).")
            return
        discord_id = str(member.id)
        if sc2_name:
            sc2_name = sc2_name.strip()
            if self.store.unlink_player(discord_id, sc2_name):
                await ctx.send(f"Unlinked **{sc2_name}** from {member.display_name}.")
            else:
                await ctx.send(f"{member.display_name} doesn't have **{sc2_name}** linked.")
            return
        names = self.store.sc2_names_for(discord_id)
        if not names:
            await ctx.send(f"{member.display_name} has no linked accounts.")
            return
        for name in names:
            self.store.unlink_player(discord_id, name)
        await ctx.send(f"Unlinked {member.display_name}'s accounts: " + ", ".join(f"**{n}**" for n in names))

    @commands.hybrid_command(help="unlink one of your SC2 names")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def unlink(self, ctx, *, sc2_name: str):
        if self.store.unlink_player(str(ctx.author.id), sc2_name.strip()):
            await ctx.send(f"Unlinked **{sc2_name.strip()}**.")
        else:
            await ctx.send(f"You don't have **{sc2_name.strip()}** linked.")

    def _accounts_embed(self, discord_id: str, title: str) -> discord.Embed | None:
        handles = self.store.handles_for(discord_id)
        pending = self.store.pending_names_for(discord_id)
        if not handles and not pending:
            return None
        lines = []
        for handle in handles:
            aliases = self.store.aliases_for_handle(handle)
            line = f"• **{aliases[0] if aliases else '?'}**"
            if aliases[1:]:
                line += f" (also: {', '.join(aliases[1:])})"
            lines.append(line)
        for name in pending:
            lines.append(f"• **{name}** *(not yet seen in a game)*")
        return discord.Embed(title=title, description="Linked SC2 accounts:\n" + "\n".join(lines), color=ACCENT)

    @commands.hybrid_command(help="show which SC2 accounts are linked to you")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def whoami(self, ctx):
        embed = self._accounts_embed(str(ctx.author.id), ctx.author.display_name)
        if embed is None:
            await ctx.send("You haven't linked any SC2 names yet. Use `!link <your SC2 name>`.")
        else:
            await ctx.send(embed=embed)

    @commands.hybrid_command(help="look up anyone's SC2 accounts — by @member or SC2 name")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def whois(self, ctx, *, target: str):
        target = target.strip()
        try:
            member = await commands.MemberConverter().convert(ctx, target)
        except commands.BadArgument:
            member = None
        if member is not None:
            embed = self._accounts_embed(str(member.id), member.display_name)
            await ctx.send(
                embed=embed if embed else None,
                content=None if embed else f"{member.display_name} hasn't linked any SC2 accounts.",
            )
            return

        candidates = self.store.candidates_for_name(target)
        if not candidates:
            await ctx.send(f"No account has played as **{target}**.")
            return
        lines = []
        for handle, name, games in candidates:
            aliases = self.store.aliases_for_handle(handle)
            line = f"• **{aliases[0] if aliases else name}**"
            if aliases[1:]:
                line += f" (also: {', '.join(aliases[1:])})"
            line += f" — {games} games"
            disc = self.store.discord_id_for_handle(handle)
            if disc:
                line += f", linked to <@{disc}>"
            lines.append(line)
        embed = discord.Embed(title=f"Accounts matching '{target}'", description="\n".join(lines), color=ACCENT)
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def setup(client):
    await client.add_cog(Identity(client))
