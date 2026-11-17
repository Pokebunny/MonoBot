"""Replays posted by an upload webhook (uploader/): stored without prompts,
answered with a reaction, and routed to a server whose members played all
eight slots (`route_member_games_to`)."""

import asyncio
from types import SimpleNamespace

import pytest
from cogs import replays
from cogs.replays import Replays
from community import Communities
from conftest import make_player
from models.config import BotConfig
from test_storage import _match

HOME, BGH, WEBHOOK = 111, 222, 999
ROSTER = ["A0", "A1", "A2", "A3", "B0", "B1", "B2", "B3"]


class _Channel:
    def __init__(self, channel_id, guild):
        self.id = channel_id
        self.guild = guild
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(id=len(self.sent))


class _Guild:
    def __init__(self, guild_id, name, member_ids=()):
        self.id = guild_id
        self.name = name
        self.member_ids = set(member_ids)

    def get_member(self, user_id):
        return SimpleNamespace(id=user_id) if user_id in self.member_ids else None


class _Attachment:
    def __init__(self, data, filename="Monobattle LotV - Map Rotation (1).SC2Replay"):
        self.data = data
        self.filename = filename

    async def read(self):
        return self.data


class _Message:
    def __init__(self, channel, data, webhook_id=WEBHOOK):
        self.id = 1
        self.guild = channel.guild
        self.channel = channel
        self.webhook_id = webhook_id
        self.author = SimpleNamespace(id=webhook_id, bot=True, display_name="Uploader")
        self.attachments = [_Attachment(data)]
        self.reactions = []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


def _setup(tmp_path, monkeypatch, home_extra=None):
    monkeypatch.setattr("community.DEFAULT_DB_PATH", str(tmp_path / "monobot.db"))
    monkeypatch.setattr(replays, "REPLAY_ARCHIVE_DIR", str(tmp_path / "replays"))
    config = BotConfig.model_validate(
        {
            "home_guild_id": HOME,
            "replays_channel_ids": [10],
            "upload_webhook_ids": [WEBHOOK],
            "route_member_games_to": [BGH],
            **(home_extra or {}),
            "guilds": {BGH: {"replays_channel_ids": [20]}},
        }
    )
    communities = Communities(config, guilds_dir=str(tmp_path / "guilds"))
    guilds = {HOME: _Guild(HOME, "Mono"), BGH: _Guild(BGH, "BGH")}
    channels = {10: _Channel(10, guilds[HOME]), 20: _Channel(20, guilds[BGH])}
    client = SimpleNamespace(
        communities=communities, get_channel=channels.get, get_guild=guilds.get, get_cog=lambda _name: None
    )
    for i, name in enumerate(ROSTER):
        communities.home.store.add_account(str(1000 + i), f"h-{name}")
    return client, communities, guilds, channels


@pytest.fixture
def setup(tmp_path, monkeypatch):
    client, communities, guilds, channels = _setup(tmp_path, monkeypatch)
    yield client, communities, guilds, channels
    for community in communities.all():
        community.store.close()


def _post(client, channel, match, data=b"replay", webhook_id=WEBHOOK, monkeypatch=None):
    monkeypatch.setattr(replays, "_parse_bytes", lambda _data, _name: match)
    message = _Message(channel, data, webhook_id)
    asyncio.run(Replays(client).on_message(message))
    return message


def test_all_bgh_members_go_to_bgh(setup, monkeypatch):
    client, communities, guilds, channels = setup
    guilds[BGH].member_ids = {1000 + i for i in range(8)}
    message = _post(client, channels[10], _match(), monkeypatch=monkeypatch)
    assert message.reactions == ["✅"]
    assert communities.others[BGH].store.match_count() == 1
    assert communities.home.store.match_count() == 0
    assert channels[20].sent and not channels[10].sent


def test_one_outsider_keeps_it_on_the_main_server(setup, monkeypatch):
    client, communities, guilds, channels = setup
    guilds[BGH].member_ids = {1000 + i for i in range(7)}
    _post(client, channels[10], _match(), monkeypatch=monkeypatch)
    assert communities.home.store.match_count() == 1
    assert communities.others[BGH].store.match_count() == 0


def test_an_unlinked_player_keeps_it_on_the_main_server(setup, monkeypatch):
    client, communities, guilds, channels = setup
    guilds[BGH].member_ids = {1000 + i for i in range(8)}
    _post(client, channels[10], _match(roster=[*ROSTER[:7], "Stranger"]), monkeypatch=monkeypatch)
    assert communities.home.store.match_count() == 1


def test_a_routed_game_still_reaches_collectors(tmp_path, monkeypatch):
    client, communities, guilds, channels = _setup(tmp_path, monkeypatch, {"counts_games_from": [BGH]})
    guilds[BGH].member_ids = {1000 + i for i in range(8)}
    _post(client, channels[10], _match(), monkeypatch=monkeypatch)
    assert communities.others[BGH].store.match_count() == communities.home.store.match_count() == 1
    for community in communities.all():
        community.store.close()


def test_a_second_upload_is_silent(setup, monkeypatch):
    client, communities, _, channels = setup
    _post(client, channels[10], _match(), monkeypatch=monkeypatch)
    sent = len(channels[10].sent)
    again = _post(client, channels[10], _match(), data=b"other recording", monkeypatch=monkeypatch)
    assert again.reactions == ["🔁"]
    assert len(channels[10].sent) == sent
    assert communities.home.store.match_count() == 1


def test_a_non_4v4_is_skipped_quietly(setup, monkeypatch):
    client, communities, _, channels = setup
    match = _match()
    match = match.model_copy(update={"players": [make_player(f"P{i}", 1 + i % 2) for i in range(6)]})
    message = _post(client, channels[10], match, monkeypatch=monkeypatch)
    assert message.reactions == ["⏭️"]
    assert communities.home.store.match_count() == 0
    assert channels[10].sent == []


def test_other_webhooks_are_ignored(setup, monkeypatch):
    client, communities, _, channels = setup
    message = _post(client, channels[10], _match(), webhook_id=123, monkeypatch=monkeypatch)
    assert message.reactions == []
    assert communities.home.store.match_count() == 0
