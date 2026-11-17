"""The replay upload API (cogs/uploads.py): token auth, quiet storing, and
routing to a server whose linked members played enough of the slots."""

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cogs import replays
from cogs.replays import Replays
from cogs.uploads import make_app
from community import Communities
from conftest import make_player
from models.config import BotConfig
from test_storage import _match

BGH, MONO = 111, 222
ROSTER = ["A0", "A1", "A2", "A3", "B0", "B1", "B2", "B3"]
UPLOADER = "5000"


class _Channel:
    def __init__(self, channel_id, guild):
        self.id = channel_id
        self.guild = guild
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(id=len(self.sent))


class _Guild:
    def __init__(self, guild_id, name):
        self.id = guild_id
        self.name = name
        self.member_ids = set()

    def get_member(self, user_id):
        return SimpleNamespace(id=user_id) if user_id in self.member_ids else None


@pytest.fixture
def bot(tmp_path, monkeypatch):
    """Home = BGH; the Monobattle server collects BGH's games, as live."""
    monkeypatch.setattr("community.DEFAULT_DB_PATH", str(tmp_path / "monobot.db"))
    monkeypatch.setattr(replays, "REPLAY_ARCHIVE_DIR", str(tmp_path / "replays"))
    config = BotConfig.model_validate(
        {
            "home_guild_id": BGH,
            "replays_channel_ids": [10],
            "upload_api": {"port": 0, "default_guild_id": MONO},
            "guilds": {MONO: {"replays_channel_ids": [20], "counts_games_from": [BGH], "route_member_games_to": [BGH]}},
        }
    )
    communities = Communities(config, guilds_dir=str(tmp_path / "guilds"))
    guilds = {BGH: _Guild(BGH, "BGH"), MONO: _Guild(MONO, "Mono")}
    channels = {10: _Channel(10, guilds[BGH]), 20: _Channel(20, guilds[MONO])}
    client = SimpleNamespace(
        communities=communities,
        get_channel=channels.get,
        get_guild=guilds.get,
        get_user=lambda user_id: SimpleNamespace(id=user_id, display_name=f"user{user_id}"),
        is_ready=lambda: True,
    )
    client.get_cog = lambda name: Replays(client) if name == "Replays" else None
    for i, name in enumerate(ROSTER):
        communities.home.store.add_account(str(1000 + i), f"h-{name}")
    token = communities.home.store.new_upload_token(UPLOADER)
    yield SimpleNamespace(client=client, communities=communities, guilds=guilds, channels=channels, token=token)
    for community in communities.all():
        community.store.close()


def _call(bot, method, path, token=None, data=None, headers=None):
    async def run():
        async with TestClient(TestServer(make_app(bot.client))) as http:
            auth = {"Authorization": f"Bearer {token or bot.token}", **(headers or {})}
            response = await http.request(method, path, data=data, headers=auth)
            return response.status, await response.json()

    return asyncio.run(run())


def _upload(bot, monkeypatch, match, data=b"replay", token=None):
    monkeypatch.setattr(replays, "_parse_bytes", lambda _data, _name: match)
    return _call(bot, "POST", "/v1/replays", token, data, {"X-Replay-Name": "Monobattle%20(1).SC2Replay"})


def test_a_token_names_its_owner(bot):
    assert _call(bot, "GET", "/v1/me") == (200, {"user": f"user{UPLOADER}"})


def test_an_unknown_token_is_refused(bot, monkeypatch):
    status, _ = _upload(bot, monkeypatch, _match(), token="nope")
    assert status == 401
    assert bot.communities.others[MONO].store.match_count() == 0


def test_a_new_token_replaces_the_old(bot):
    bot.communities.home.store.new_upload_token(UPLOADER)
    assert _call(bot, "GET", "/v1/me")[0] == 401


def test_a_game_with_six_bgh_members_goes_to_bgh(bot, monkeypatch):
    bot.guilds[BGH].member_ids = {1000 + i for i in range(6)}
    status, body = _upload(bot, monkeypatch, _match())
    assert (status, body["status"], body["server"], body["match"]) == (200, "stored", "BGH", 1)
    assert bot.communities.home.store.match_count() == 1
    # ...and reaches the Monobattle server through counts_games_from.
    assert bot.communities.others[MONO].store.match_count() == 1
    assert bot.communities.home.store.upload_count(UPLOADER) == 1


def test_a_pub_with_five_bgh_members_stays_on_mono(bot, monkeypatch):
    bot.guilds[BGH].member_ids = {1000 + i for i in range(5)}
    _, body = _upload(bot, monkeypatch, _match())
    assert (body["status"], body["server"]) == ("stored", "Mono")
    assert bot.communities.home.store.match_count() == 0
    assert bot.communities.others[MONO].store.match_count() == 1


def test_a_second_recording_is_a_quiet_duplicate(bot, monkeypatch):
    _upload(bot, monkeypatch, _match())
    sent = len(bot.channels[20].sent)
    _, body = _upload(bot, monkeypatch, _match(), data=b"another player's recording")
    assert body["status"] == "duplicate"
    assert len(bot.channels[20].sent) == sent


def test_the_desktop_uploader_speaks_this_api(bot, monkeypatch):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "uploader"))
    import monobot_uploader

    names = []
    monkeypatch.setattr(replays, "_parse_bytes", lambda _data, name: names.append(name) or _match())

    async def run():
        async with TestServer(make_app(bot.client)) as server:
            url = str(server.make_url(""))
            user = await asyncio.to_thread(monobot_uploader.check_token, url, bot.token)
            result = await asyncio.to_thread(
                monobot_uploader.post_replay, url, bot.token, "Mönobattle (2).SC2Replay", b"r"
            )
            return user, result

    user, result = asyncio.run(run())
    assert user == f"user{UPLOADER}"
    assert result["status"] == "stored"
    assert names == ["Mönobattle (2).SC2Replay"]


def test_a_non_4v4_is_rejected_without_a_post(bot, monkeypatch):
    match = _match().model_copy(update={"players": [make_player(f"P{i}", 1 + i % 2) for i in range(6)]})
    _, body = _upload(bot, monkeypatch, match)
    assert body["status"] == "rejected" and "4v4" in body["reason"]
    assert bot.channels[20].sent == []
