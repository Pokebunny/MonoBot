import discord
from discord.ext import commands


class Misc(commands.Cog):
    def __init__(self, client):
        self.client = client

    @commands.hybrid_command(help="check that the bot is alive")
    @commands.cooldown(1, 2, commands.BucketType.user)
    async def ping(self, ctx):
        embed = discord.Embed(
            title="Pong!",
            description=f"Latency: {self.client.latency * 1000:.0f} ms",
        )
        await ctx.send(embed=embed)

    @commands.command(help="sync slash commands with Discord (owner only)")
    @commands.is_owner()
    async def sync(self, ctx):
        synced = await self.client.tree.sync()
        await ctx.send(f"Synced {len(synced)} slash commands.")


async def setup(client):
    await client.add_cog(Misc(client))
