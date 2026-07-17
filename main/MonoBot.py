# file: MonoBot.py

import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()
token = os.getenv("BOT_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

cog_files = [
    "misc",
    "identity",
    "replays",
    "leaderboard",
    "matchmaking",
]


class MonoBot(commands.Bot):
    async def setup_hook(self):
        for cog in cog_files:
            await self.load_extension("cogs." + cog)
            logger.info("%s cog loaded", cog)


bot = MonoBot(command_prefix="!", intents=intents, case_insensitive=True)


@bot.event
async def on_ready():
    logger.info("%s has connected to Discord!", bot.user)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return

    logger.warning("Command error in %s: %s", ctx.command, error)

    if isinstance(error, (commands.CommandOnCooldown, commands.MissingRole)):
        embed = discord.Embed(title=str(error), color=0xEA7D07)
        await ctx.send(embed=embed)
    else:
        embed = discord.Embed(title="Something went wrong!", color=0xEA7D07)
        embed.add_field(name="Error:", value=str(error), inline=True)
        await ctx.send(embed=embed)


if __name__ == "__main__":
    # root_logger=True lets discord.py configure the root logger so our own
    # module loggers are emitted, not just discord's.
    bot.run(token, root_logger=True)
