"""Link Discord users to the SC2 name(s) they play under.

Winner confirmation (in the replays cog) is restricted to players who were in
the match, and that check runs through these links: a member can only confirm
a game if one of their linked SC2 names is in it.
"""

import logging

import discord
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


class Identity(commands.Cog):
    def __init__(self, client):
        self.client = client
        if not hasattr(client, "match_store"):
            client.match_store = MatchStore()
        self.store: MatchStore = client.match_store

    @commands.hybrid_command(help="link your Discord account to your in-game SC2 name")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def link(self, ctx, *, sc2_name: str):
        sc2_name = sc2_name.strip()
        if not sc2_name:
            await ctx.send("Usage: `!link <your SC2 name>`")
            return

        result = self.store.link_player(str(ctx.author.id), sc2_name)
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

    @commands.hybrid_command(help="link an additional SC2 account to yourself (e.g. a different region)")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def addaccount(self, ctx, *, sc2_name: str):
        sc2_name = sc2_name.strip()
        candidates = self.store.candidates_for_name(sc2_name)
        if not candidates:
            await ctx.send(f"No account has played as **{sc2_name}** in the match history yet.")
            return
        embed = discord.Embed(
            title="Add which account?",
            description=f"Pick the **{sc2_name}** account to add to your profile — all your accounts share one rating.",
            color=ACCENT,
        )
        await ctx.send(
            embed=embed,
            view=DisambiguationView(self.store, str(ctx.author.id), sc2_name, candidates, add_mode=True),
        )

    @commands.hybrid_command(help="unlink one of your SC2 names")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def unlink(self, ctx, *, sc2_name: str):
        if self.store.unlink_player(str(ctx.author.id), sc2_name.strip()):
            await ctx.send(f"Unlinked **{sc2_name.strip()}**.")
        else:
            await ctx.send(f"You don't have **{sc2_name.strip()}** linked.")

    @commands.hybrid_command(help="show which SC2 accounts are linked to you")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def whoami(self, ctx):
        uid = str(ctx.author.id)
        handles = self.store.handles_for(uid)
        pending = self.store.pending_names_for(uid)
        if not handles and not pending:
            await ctx.send("You haven't linked any SC2 names yet. Use `!link <your SC2 name>`.")
            return

        lines = []
        for handle in handles:
            aliases = self.store.aliases_for_handle(handle)
            current = aliases[0] if aliases else "?"
            others = aliases[1:]
            line = f"• **{current}**"
            if others:
                line += f" (also: {', '.join(others)})"
            lines.append(line)
        for name in pending:
            lines.append(f"• **{name}** *(not yet seen in a game)*")

        embed = discord.Embed(
            title=ctx.author.display_name,
            description="Linked SC2 accounts:\n" + "\n".join(lines),
            color=ACCENT,
        )
        await ctx.send(embed=embed)


async def setup(client):
    await client.add_cog(Identity(client))
