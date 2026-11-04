"""A community that counts another server's games (`counts_games_from`):
every upload there is stored and posted here too, one way only, and a winner
confirmed in either settles both copies."""

import asyncio
from types import SimpleNamespace

import pytest
from cogs import replays
from cogs.replays import Replays, settle_copy
from community import Communities
from models.config import BotConfig
from services.storage import hash_replay
from test_storage import _match

HOME, OTHER = 111, 222


class _Channel:
    def __init__(self, channel_id, guild):
        self.id = channel_id
        self.guild = guild
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(id=len(self.sent))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("community.DEFAULT_DB_PATH", str(tmp_path / "monobot.db"))
    monkeypatch.setattr(replays, "REPLAY_ARCHIVE_DIR", str(tmp_path / "replays"))
    config = BotConfig.model_validate(
        {
            "home_guild_id": HOME,
            "replays_channel_ids": [10],
            "guilds": {OTHER: {"replays_channel_ids": [20], "counts_games_from": [HOME]}},
        }
    )
    communities = Communities(config, guilds_dir=str(tmp_path / "guilds"))
    channels = {
        10: _Channel(10, SimpleNamespace(id=HOME, name="Home")),
        20: _Channel(20, SimpleNamespace(id=OTHER, name="Other")),
    }
    client = SimpleNamespace(communities=communities, get_channel=channels.get, get_cog=lambda _name: None)
    yield client, communities.home, communities.others[OTHER], channels
    for community in communities.all():
        community.store.close()


def _upload(client, community, channel, match, data=b"replay"):
    cog = Replays(client).at(community)
    asyncio.run(cog._record(channel, match, data, hash_replay(data), SimpleNamespace(id=7, display_name="up")))


def test_only_the_collector_is_linked(setup):
    client, home, other, _ = setup
    assert client.communities.collectors_of(home) == [other]
    assert client.communities.collectors_of(other) == []
    assert client.communities.sharing_games_with(other) == [home]


def test_an_upload_counts_and_posts_in_both(setup):
    client, home, other, channels = setup
    _upload(client, home, channels[10], _match())
    assert home.store.match_count() == other.store.match_count() == 1
    # The summary and the badges it unlocks, in each server.
    assert len(channels[10].sent) == len(channels[20].sent) == 2
    assert "uploaded in Home" in channels[20].sent[0]["embed"].footer.text
    assert other.store.upload_count("7") == 1  # credited to the uploader there too


def test_it_goes_one_way(setup):
    client, home, other, channels = setup
    _upload(client, other, channels[20], _match())
    assert other.store.match_count() == 1
    assert home.store.match_count() == 0
    assert channels[10].sent == []


def test_a_game_the_collector_already_has_goes_unmentioned_there(setup):
    client, home, other, channels = setup
    other.store.ingest(_match(), hash_replay(b"replay"))
    _upload(client, home, channels[10], _match())
    assert home.store.match_count() == 1
    assert channels[20].sent == []  # no "already stored" notice for a game uploaded elsewhere


def test_a_confirmed_winner_settles_the_other_copy(setup):
    client, home, other, channels = setup
    unsure = _match(winning_team=None, confidence=0.0)
    _upload(client, home, channels[10], unsure)
    asyncio.run(settle_copy(client, other, unsure, 2))
    copy = other.store.get_match(other.store.find_game(unsure)["id"])
    assert copy.winning_team == 2
    assert copy.winner_method == "confirmed"
