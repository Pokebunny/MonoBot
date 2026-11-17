"""Recording a rejected game anyway is for admins only, not its players."""

import asyncio
from types import SimpleNamespace

import discord
import pytest
from cogs import replays
from cogs.replays import ApproveGameView, Replays
from community import Communities
from models.config import BotConfig
from test_storage import _match

HOME, ADMIN, PLAYER = 111, 1, 2


class _Response:
    def __init__(self):
        self.private = []

    async def send_message(self, content, ephemeral=False):
        self.private.append(content)

    async def edit_message(self, **kwargs):
        pass


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("community.DEFAULT_DB_PATH", str(tmp_path / "monobot.db"))
    monkeypatch.setattr(replays, "REPLAY_ARCHIVE_DIR", str(tmp_path / "replays"))
    config = BotConfig.model_validate({"home_guild_id": HOME, "replays_channel_ids": [10], "admin_user_ids": [ADMIN]})
    communities = Communities(config, guilds_dir=str(tmp_path / "guilds"))
    channel = SimpleNamespace(id=10, guild=SimpleNamespace(id=HOME, name="Home"), send=None)

    async def send(**kwargs):
        return SimpleNamespace(id=1)

    channel.send = send
    client = SimpleNamespace(communities=communities, get_channel=lambda _id: channel, get_cog=lambda _name: None)
    # The player is linked to an account that played in the game.
    communities.home.store.add_account(str(PLAYER), "h-A0")
    yield client, communities.home, channel
    communities.home.store.close()


def _press(client, community, channel, user_id):
    match = _match(duration=60)  # a remake: rejected
    response = _Response()

    async def run():
        view = ApproveGameView(Replays(client).at(community), match, b"replay", "hash", SimpleNamespace(id=PLAYER))
        interaction = SimpleNamespace(
            client=client,
            guild=channel.guild,
            channel=channel,
            user=SimpleNamespace(id=user_id, display_name="someone", guild_permissions=None),
            response=response,
            message=SimpleNamespace(embeds=[discord.Embed(title="Not recorded")]),
        )
        await view.approve.callback(interaction)

    asyncio.run(run())
    return response


def test_a_player_in_the_game_cannot_record_it(setup):
    client, community, channel = setup
    response = _press(client, community, channel, PLAYER)
    assert response.private == ["Only an admin can record a rejected game."]
    assert community.store.match_count() == 0


def test_an_admin_can(setup):
    client, community, channel = setup
    response = _press(client, community, channel, ADMIN)
    assert response.private == []
    assert community.store.match_count() == 1
