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

logger = logging.getLogger(__name__)


class _AccountSelect(discord.ui.Select):
    """Dropdown to pick which account a shared name belongs to. In add_mode it
    attaches an ADDITIONAL account to the user (multi-account merge)."""

    def __init__(self, store: MatchStore, discord_id: str, sc2_name: str, candidates, add_mode: bool = False):
        self.store = store
        self.discord_id = discord_id
        self.sc2_name = sc2_name
        self.add_mode = add_mode
        options = [
            discord.SelectOption(label=name[:100], description=f"{games} games · …{handle[-6:]}", value=handle)
            for handle, name, games in candidates[:25]
        ]
        super().__init__(placeholder="Which account is yours?", options=options)

    async def callback(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.discord_id:
            await interaction.response.send_message("Only the person linking can choose here.", ephemeral=True)
            return
        if self.add_mode:
            ok = self.store.add_account(self.discord_id, self.values[0])
            msg = (
                "Added that account — all your accounts now share one rating."
                if ok
                else "That account is already linked to someone else — ask an admin."
            )
        else:
            ok = self.store.bind_specific(self.discord_id, self.sc2_name, self.values[0])
            msg = (
                f"Linked **{self.sc2_name}** to your account."
                if ok
                else "That account is already linked to someone else — ask an admin."
            )
        await interaction.response.edit_message(content=msg, embed=None, view=None)


class DisambiguationView(discord.ui.View):
    def __init__(self, store: MatchStore, discord_id: str, sc2_name: str, candidates, add_mode: bool = False):
        super().__init__(timeout=120)
        self.add_item(_AccountSelect(store, discord_id, sc2_name, candidates, add_mode))


class _PairPickSelect(discord.ui.Select):
    """One dropdown of a PairPickView — picks which account a name means."""

    def __init__(self, parent: "PairPickView", slot: int, sc2_name: str, candidates):
        self.parent_view = parent
        self.slot = slot
        options = [
            discord.SelectOption(label=name[:100], description=f"{games} games · …{handle[-6:]}", value=handle)
            for handle, name, games in candidates[:25]
        ]
        super().__init__(placeholder=f"Which account is '{sc2_name}'?", options=options)

    async def callback(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.parent_view.admin_id:
            await interaction.response.send_message("Only the person who ran the command can choose.", ephemeral=True)
            return
        self.parent_view.picks[self.slot] = self.values[0]
        for option in self.options:
            option.default = option.value == self.values[0]
        await self.parent_view.maybe_finish(interaction)


class PairPickView(discord.ui.View):
    """Account pickers for (un)merge when a name matches several accounts.
    Runs the merge/unmerge once every ambiguous name has been resolved."""

    def __init__(self, store: MatchStore, admin_id: str, action: str, names, candidate_lists):
        super().__init__(timeout=120)
        self.store = store
        self.admin_id = admin_id
        self.action = action  # "merge" | "unmerge"
        self.names = names
        self.picks = [cands[0][0] if len(cands) == 1 else None for cands in candidate_lists]
        for slot, (name, cands) in enumerate(zip(names, candidate_lists)):
            if len(cands) > 1:
                self.add_item(_PairPickSelect(self, slot, name, cands))

    async def maybe_finish(self, interaction: discord.Interaction):
        if None in self.picks:
            await interaction.response.edit_message(view=self)
            return
        h1, h2 = self.picks
        name1, name2 = self.names
        tag1, tag2 = f"**{name1}** (…{h1[-6:]})", f"**{name2}** (…{h2[-6:]})"
        if self.action == "merge":
            if h1 == h2:
                content = "You picked the same account twice — nothing to merge."
            else:
                merge_map = self.store.merge_map()
                if merge_map.get(h1, h1) == merge_map.get(h2, h2):
                    content = f"{tag1} and {tag2} are already the same player."
                else:
                    self.store.merge_accounts(h1, h2)
                    content = f"Merged {tag1} and {tag2} — their games now share one rating."
        else:
            if self.store.unmerge_accounts(h1, h2):
                content = f"Un-merged {tag1} and {tag2}."
            else:
                content = (
                    f"{tag1} and {tag2} weren't merged by an admin (a shared Discord link can't be un-merged here)."
                )
        await interaction.response.edit_message(content=content, embed=None, view=None)


class ConfirmAddView(discord.ui.View):
    """Shown when someone links a name while already having an account linked —
    confirms they mean to add a second (merged) account, not replace."""

    def __init__(self, cog: "Identity", discord_id: str, sc2_name: str):
        super().__init__(timeout=120)
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

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._mine(interaction):
            await interaction.response.edit_message(content="Cancelled — no account added.", embed=None, view=None)


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
            await interaction.response.edit_message(
                embed=embed, view=DisambiguationView(self.store, discord_id, sc2_name, candidates, add_mode=True)
            )

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
            await ctx.send(embed=embed, view=ConfirmAddView(self, discord_id, sc2_name))
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
            await ctx.send(
                embed=embed,
                view=DisambiguationView(self.store, str(ctx.author.id), sc2_name, candidates),
            )
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

    @commands.hybrid_command(help="link another member to an SC2 account (mods)")
    @is_bot_admin()
    async def linkuser(self, ctx, member: discord.Member, *, sc2_name: str):
        sc2_name = sc2_name.strip()
        discord_id = str(member.id)
        candidates = self.store.candidates_for_name(sc2_name)
        if not candidates:
            result = self.store.link_player(discord_id, sc2_name)
            if result.status == "taken":
                await ctx.send(f"**{sc2_name}** is already linked to another member.")
            else:
                await ctx.send(f"Linked **{member.display_name}** to **{sc2_name}** — it'll bind on their next game.")
            return
        if not self.store.bind_specific(discord_id, sc2_name, candidates[0][0]):
            await ctx.send(f"**{sc2_name}** (or that account) is already linked to another member.")
            return
        extra = f" (matched {len(candidates)} accounts — used the most active)" if len(candidates) > 1 else ""
        await ctx.send(f"Linked **{member.display_name}** to **{sc2_name}**.{extra}")

    async def _merge_pair(self, ctx, name1: str, name2: str, action: str):
        """Shared (un)merge flow: resolve both names, asking with dropdowns
        when a name matches several accounts."""
        c1 = self.store.candidates_for_name(name1)
        c2 = self.store.candidates_for_name(name2)
        if not c1:
            await ctx.send(f"No account has played as **{name1}** yet.")
            return
        if not c2:
            await ctx.send(f"No account has played as **{name2}** yet.")
            return
        view = PairPickView(self.store, str(ctx.author.id), action, (name1, name2), (c1, c2))
        if None not in view.picks:  # both names unambiguous — no picker needed
            h1, h2 = view.picks
            if action == "merge":
                merge_map = self.store.merge_map()
                if merge_map.get(h1, h1) == merge_map.get(h2, h2):
                    await ctx.send(f"**{name1}** and **{name2}** are already the same player.")
                    return
                self.store.merge_accounts(h1, h2)
                await ctx.send(f"Merged **{name1}** and **{name2}** — their games now share one rating.")
            elif self.store.unmerge_accounts(h1, h2):
                await ctx.send(f"Un-merged **{name1}** and **{name2}**.")
            else:
                await ctx.send("Those two weren't merged by an admin (a shared Discord link can't be un-merged here).")
            return
        ambiguous = [n for n, c in ((name1, c1), (name2, c2)) if len(c) > 1]
        embed = discord.Embed(
            title="Which accounts?",
            description=", ".join(f"**{n}**" for n in dict.fromkeys(ambiguous))
            + " matches several accounts — pick the ones you mean (by game count / account id). "
            "To merge two accounts that share a name, use the same name twice and pick a different one in each menu.",
            color=WARNING,
        )
        await ctx.send(embed=embed, view=view)

    @commands.hybrid_command(help="declare two SC2 accounts the same player (mods)")
    @is_bot_admin()
    async def mergeaccounts(self, ctx, name1: str, name2: str):
        await self._merge_pair(ctx, name1, name2, "merge")

    @commands.hybrid_command(help="undo an account merge (mods)")
    @is_bot_admin()
    async def unmergeaccounts(self, ctx, name1: str, name2: str):
        await self._merge_pair(ctx, name1, name2, "unmerge")

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
