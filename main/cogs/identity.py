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
            embed = discord.Embed(
                title="Name needs disambiguation",
                description=f"**{result.candidates} different accounts** have played as **{sc2_name}**, "
                "so I can't tell which one is you. Your claim is saved, but an admin will need to "
                "bind it to the right account.",
                color=WARNING,
            )
            await ctx.send(embed=embed)
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

    @commands.hybrid_command(help="unlink one of your SC2 names")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def unlink(self, ctx, *, sc2_name: str):
        if self.store.unlink_player(str(ctx.author.id), sc2_name.strip()):
            await ctx.send(f"Unlinked **{sc2_name.strip()}**.")
        else:
            await ctx.send(f"You don't have **{sc2_name.strip()}** linked.")

    @commands.hybrid_command(help="show which SC2 names are linked to you")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def whoami(self, ctx):
        names = self.store.sc2_names_for(str(ctx.author.id))
        if not names:
            await ctx.send("You haven't linked any SC2 names yet. Use `!link <your SC2 name>`.")
            return
        embed = discord.Embed(
            title=ctx.author.display_name,
            description="Linked SC2 names:\n" + "\n".join(f"• **{n}**" for n in names),
            color=ACCENT,
        )
        await ctx.send(embed=embed)


async def setup(client):
    await client.add_cog(Identity(client))
