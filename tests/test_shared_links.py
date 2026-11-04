"""Discord links are global: every community's store reads and writes the
one table in the home DB."""

import sqlite3

import pytest
from services.storage import MatchStore, SharedLinks


@pytest.fixture
def paths(tmp_path):
    return str(tmp_path / "home.db"), str(tmp_path / "guild.db")


@pytest.fixture
def stores(paths):
    home_path, guild_path = paths
    links = SharedLinks(home_path)
    home, guild = MatchStore(home_path, links), MatchStore(guild_path, links)
    yield home, guild
    home.close()
    guild.close()


def test_a_link_made_on_one_server_counts_on_the_other(stores):
    home, guild = stores
    assert guild.link_player("42", "Rain").status == "linked"
    assert home.discord_id_for("rain") == "42"
    assert home.add_account("42", "h-2")
    assert guild.handles_for("42") == ["h-2"]
    assert home.link_player("7", "Rain").status == "taken"


def test_unlinking_anywhere_unlinks_everywhere(stores):
    home, guild = stores
    home.link_player("42", "Rain")
    assert guild.unlink_player("42", "Rain")
    assert home.discord_id_for("Rain") is None


def test_link_writes_invalidate_every_stores_caches(stores):
    home, guild = stores
    before = home.change_count, guild.change_count
    guild.link_player("42", "Rain")
    assert home.change_count > before[0] and guild.change_count > before[1]


def test_merges_come_from_the_shared_links(stores):
    home, guild = stores
    home.add_account("42", "h-1")
    home.add_account("42", "h-2")
    assert guild.merge_map() == home.merge_map()
    assert len(set(guild.merge_map().values())) == 1


def test_skill_levels_stay_per_server_but_join_the_shared_links(stores):
    home, guild = stores
    home.add_account("42", "h-1")
    guild.set_skill_level("42", "top")
    assert guild.skill_levels_by_handle() == {"h-1": "top"}
    assert home.skill_levels_by_handle() == {}


def test_guild_db_never_gets_its_own_links_table(paths, stores):
    tables = sqlite3.connect(paths[1]).execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    assert ("player_links",) not in tables


def test_a_servers_old_links_move_into_the_shared_table(paths):
    home_path, guild_path = paths
    # Before links were global, each server kept its own.
    home = MatchStore(home_path)
    home.link_player("1", "Taken")
    home.link_player("42", "Same")
    home.close()
    old = MatchStore(guild_path)
    old.link_player("42", "Same")  # already shared: nothing to do
    old.link_player("2", "Taken")  # clashes with someone else's claim
    old.link_player("3", "New")
    old.close()

    links = SharedLinks(home_path)
    home, guild = MatchStore(home_path, links), MatchStore(guild_path, links)
    assert home.discord_id_for("New") == "3"
    assert home.discord_id_for("Taken") == "1"  # the shared claim wins
    assert guild.discord_id_for("Taken") == "1"
    kept = sqlite3.connect(guild_path).execute("SELECT discord_id, sc2_name FROM player_links_premerge").fetchall()
    assert sorted(kept) == [("2", "Taken"), ("3", "New"), ("42", "Same")]  # nothing is lost
    guild.close()

    again = MatchStore(guild_path, links)  # idempotent
    assert again.discord_id_for("New") == "3"
    again.close()
    home.close()
