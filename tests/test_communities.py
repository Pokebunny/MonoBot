"""One ladder per Discord server: which community a server gets, and that
communities never see each other's data."""

import types

import pytest
from cogs.leaderboard import Leaderboard
from cogs.matchmaking import Matchmaking
from community import Communities, daily_time
from models.config import BotConfig

HOME, OTHER, STRANGER = 111, 222, 333


@pytest.fixture
def communities(tmp_path, monkeypatch):
    monkeypatch.setattr("community.DEFAULT_DB_PATH", str(tmp_path / "monobot.db"))
    config = BotConfig.model_validate_json(
        f'{{"home_guild_id": {HOME}, "admin_user_ids": [1],'
        f' "guilds": {{"{OTHER}": {{"replays_channel_ids": [9], "recap_timezone": "Europe/Berlin",'
        f' "admin_user_ids": [2]}}}}}}'
    )
    found = Communities(config, guilds_dir=str(tmp_path / "guilds"))
    yield found
    for community in found.all():
        community.store.close()


def _guild(guild_id):
    return types.SimpleNamespace(id=guild_id)


def test_config_reads_guilds_keyed_by_id(communities):
    other = communities.for_guild(_guild(OTHER))
    assert other.config.replays_channel_ids == [9]
    assert other.config.recap_timezone == "Europe/Berlin"
    assert other.config.queue_reset_time == "05:00"  # unset fields take the defaults


def test_each_server_gets_its_own_community(communities):
    home, other = communities.for_guild(_guild(HOME)), communities.for_guild(_guild(OTHER))
    assert home is communities.home and other is not home
    assert communities.for_guild(OTHER) is other  # a bare id works too


def test_an_unconfigured_server_gets_no_ladder(communities):
    assert communities.for_guild(_guild(STRANGER)) is None


def test_dms_have_no_ladder_once_there_are_two(communities):
    assert communities.for_guild(None) is None


def test_legacy_config_serves_every_server_from_home(tmp_path, monkeypatch):
    """No home_guild_id and no guilds: the bot behaves as it did before
    communities existed, so deploying this changes nothing by itself."""
    monkeypatch.setattr("community.DEFAULT_DB_PATH", str(tmp_path / "monobot.db"))
    found = Communities(BotConfig(), guilds_dir=str(tmp_path / "guilds"))
    assert found.for_guild(_guild(STRANGER)) is found.home
    assert found.for_guild(None) is found.home
    found.home.store.close()


def test_communities_keep_separate_databases(communities, tmp_path):
    home, other = communities.home, communities.others[OTHER]
    home.store.link_player("42", "Somebody")
    assert home.store.pending_names_for("42") == ["Somebody"]
    assert other.store.pending_names_for("42") == []
    assert (tmp_path / "guilds" / str(OTHER) / "monobot.db").exists()


def test_admins_are_global_or_per_community(communities):
    assert communities.is_admin(1)
    assert communities.others[OTHER].is_admin(2)
    assert not communities.home.is_admin(2)  # a guild's own admin is admin only there


def test_a_bound_cog_reads_only_its_community(communities):
    client = types.SimpleNamespace(communities=communities)
    cog = Leaderboard(client)
    home, other = cog.at(_ctx(HOME)), cog.at(_ctx(OTHER))
    assert home.store is communities.home.store and other.store is communities.others[OTHER].store
    assert not hasattr(cog, "store")  # the shared cog itself belongs to no community


def test_each_community_has_its_own_queue(communities):
    cog = Matchmaking(types.SimpleNamespace(communities=communities))
    home, other = cog.lobby_for(_guild(HOME)), cog.lobby_for(_guild(OTHER))
    home.queue["1"] = object()
    assert other is not home and other.queue == {}
    assert cog.lobby_for(_guild(HOME)) is home  # made once, then reused
    assert cog.lobby_for(_guild(STRANGER)) is None


def test_daily_times_are_zoned_per_community():
    at = daily_time("08:00", "Europe/Berlin", "recap")
    assert (at.hour, at.minute, str(at.tzinfo)) == (8, 0, "Europe/Berlin")
    assert daily_time("8am", "Europe/Berlin", "recap") is None  # bad value: disabled, not a crash
    assert daily_time(None, "UTC", "recap") is None


def _ctx(guild_id):
    return types.SimpleNamespace(guild=_guild(guild_id))


def test_a_community_posts_only_in_its_own_server(communities):
    other = communities.others[OTHER]
    assert other.owns(types.SimpleNamespace(guild=_guild(OTHER)))
    assert not other.owns(types.SimpleNamespace(guild=_guild(HOME)))  # misfiled channel: never post there


def test_config_problems_name_what_to_fix(communities):
    other = communities.others[OTHER]  # watches channel 9 via config
    absent = types.SimpleNamespace(get_guild=lambda _id: None, get_channel=lambda _id: None)
    assert "invite it" in other.config_problems(absent)[0]
    elsewhere = types.SimpleNamespace(
        get_guild=lambda _id: object(), get_channel=lambda _id: types.SimpleNamespace(guild=_guild(HOME))
    )
    assert other.config_problems(elsewhere) == [f"replay channel 9 is in another server ({HOME})"]
    fine = types.SimpleNamespace(
        get_guild=lambda _id: object(), get_channel=lambda _id: types.SimpleNamespace(guild=_guild(OTHER))
    )
    assert other.config_problems(fine) == []
