"""The replay upload API that uploader/ posts to, and the token command.

POST /v1/replays  (Authorization: Bearer <token>, X-Replay-Name: <file name>,
body = the raw .SC2Replay) -> {"status", "server", "match", "reason"}.
GET /v1/me checks a token. Games land in upload_api.default_guild_id's
community unless Replays.route sends them elsewhere."""

import asyncio
import logging
import os
import urllib.parse
from types import SimpleNamespace

import discord
from aiohttp import web
from discord.ext import commands

logger = logging.getLogger(__name__)

# Past any real replay (they run 0.1-2 MB).
MAX_REPLAY_BYTES = 10 * 1024 * 1024
# sc2reader takes ~1-2s of CPU a replay; don't let a burst starve the bot.
MAX_CONCURRENT_PARSES = 2
BAD_TOKEN = {"error": "unknown token; get a new one with /uploadtoken"}


def upload_community(client):
    guild_id = client.communities.config.upload_api.default_guild_id
    return client.communities.home if guild_id is None else client.communities.by_id(guild_id)


def make_app(client) -> web.Application:
    app = web.Application(client_max_size=MAX_REPLAY_BYTES)
    parses = asyncio.Semaphore(MAX_CONCURRENT_PARSES)

    def owner(request) -> str | None:
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        return client.communities.home.store.upload_token_owner(token.strip())

    async def uploader(discord_id: str):
        user = client.get_user(int(discord_id))
        if user is None:
            try:
                user = await client.fetch_user(int(discord_id))
            except discord.HTTPException:
                user = SimpleNamespace(id=int(discord_id), display_name=discord_id)
        return user

    async def me(request):
        discord_id = owner(request)
        if discord_id is None:
            return web.json_response(BAD_TOKEN, status=401)
        return web.json_response({"user": (await uploader(discord_id)).display_name})

    async def upload(request):
        discord_id = owner(request)
        if discord_id is None:
            return web.json_response(BAD_TOKEN, status=401)
        if not client.is_ready():
            return web.json_response({"error": "the bot is starting; try again shortly"}, status=503)
        # URL-encoded: headers are latin-1, SC2 names needn't be.
        name = os.path.basename(urllib.parse.unquote(request.headers.get("X-Replay-Name", ""))) or "upload.SC2Replay"
        if not name.lower().endswith(".sc2replay"):
            return web.json_response({"error": "send a .SC2Replay file"}, status=400)
        data = await request.read()
        if not data:
            return web.json_response({"error": "empty body"}, status=400)
        community = upload_community(client)
        replays = client.get_cog("Replays")
        if community is None or replays is None:
            return web.json_response({"error": "uploads aren't set up on this bot"}, status=503)
        async with parses:
            outcome = await replays.at(community).api_upload(name, data, await uploader(discord_id))
        guild = client.get_guild(outcome.community.guild_id) if outcome.community.guild_id else None
        return web.json_response(
            {
                "status": outcome.status,
                "server": guild.name if guild else None,
                "match": outcome.match_number,
                "reason": outcome.reason,
            }
        )

    app.router.add_get("/v1/me", me)
    app.router.add_post("/v1/replays", upload)
    return app


class Uploads(commands.Cog):
    def __init__(self, client):
        self.client = client
        self.runner: web.AppRunner | None = None

    async def cog_load(self):
        config = self.client.communities.config.upload_api
        if config.port is None:
            return
        if upload_community(self.client) is None:
            logger.warning("upload_api.default_guild_id %s isn't a community; API off", config.default_guild_id)
            return
        self.runner = web.AppRunner(make_app(self.client), access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, config.host, config.port).start()
        logger.info("Replay upload API listening on %s:%s", config.host, config.port)

    async def cog_unload(self):
        if self.runner is not None:
            await self.runner.cleanup()

    @commands.hybrid_command(help="get a token for the automatic replay uploader (sent privately)")
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def uploadtoken(self, ctx, action: str | None = None):
        store = self.client.communities.home.store
        if action is not None and action.lower() == "revoke":
            revoked = store.revoke_upload_token(str(ctx.author.id))
            await ctx.send("Your upload token is revoked." if revoked else "You don't have an upload token.")
            return
        config = self.client.communities.config.upload_api
        if config.port is None:
            await ctx.send("The replay upload API isn't turned on for this bot.")
            return
        token = store.new_upload_token(str(ctx.author.id))
        server = config.public_url or "<ask an admin for the server address>"
        text = (
            "Your replay uploader token (it replaces any earlier one; keep it private):\n"
            f"```\npython monobot_uploader.py --server {server} --token {token}\n```"
            "Get the uploader from the MonoBot repo's `uploader/` folder. "
            "`/uploadtoken revoke` turns the token off."
        )
        if ctx.interaction is not None:
            await ctx.send(text, ephemeral=True)
            return
        try:
            await ctx.author.send(text)
        except discord.Forbidden:
            store.revoke_upload_token(str(ctx.author.id))
            await ctx.send("I couldn't DM you. Use `/uploadtoken` instead, or open your DMs.")
            return
        if ctx.guild is not None:
            await ctx.send("Sent you a DM with your upload token.")


async def setup(client):
    await client.add_cog(Uploads(client))
