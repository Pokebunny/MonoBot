import datetime

import pytest
from models.replay import MatchPlayer, MonobattleMatch
from services.storage import MatchStore, hash_replay


@pytest.fixture
def store(tmp_path):
    s = MatchStore(str(tmp_path / "test.db"))
    yield s
    s.close()


def _match(names, winning_team=None):
    players = [
        MatchPlayer(name=n, team=(1 if i < 4 else 2), race="Zerg", pick="Zergling", unit_counts={})
        for i, n in enumerate(names)
    ]
    return MonobattleMatch(
        file_name="m.SC2Replay",
        map_name="Monobattle LotV - Map Rotation",
        played_at=datetime.datetime(2026, 7, 17, tzinfo=datetime.timezone.utc),
        duration_seconds=900,
        game_type="4v4",
        pick_mode="blind_random",
        pick_phase_seconds=63,
        players=players,
        winning_team=winning_team,
        winner_confidence=0.0 if winning_team is None else 1.0,
        winner_method="unknown" if winning_team is None else "recorded",
    )


def test_link_and_lookup(store):
    assert store.link_player("disc123", "Pokebunny") is None
    assert store.sc2_names_for("disc123") == ["Pokebunny"]
    assert store.discord_id_for("Pokebunny") == "disc123"


def test_link_multiple_names(store):
    store.link_player("disc123", "Pokebunny")
    store.link_player("disc123", "Kuschelbunny")
    assert store.sc2_names_for("disc123") == ["Kuschelbunny", "Pokebunny"]


def test_name_claimed_by_other_is_rejected(store):
    store.link_player("disc123", "Pokebunny")
    owner = store.link_player("disc999", "Pokebunny")
    assert owner == "disc123"
    assert store.discord_id_for("Pokebunny") == "disc123"


def test_reclaim_own_name_is_noop_success(store):
    assert store.link_player("disc123", "Pokebunny") is None
    assert store.link_player("disc123", "Pokebunny") is None
    assert store.sc2_names_for("disc123") == ["Pokebunny"]


def test_unlink(store):
    store.link_player("disc123", "Pokebunny")
    assert store.unlink_player("disc123", "Pokebunny") is True
    assert store.sc2_names_for("disc123") == []
    # unlinking a name you don't own fails
    store.link_player("disc123", "Pokebunny")
    assert store.unlink_player("disc999", "Pokebunny") is False


def test_known_player(store):
    store.ingest(_match(["Pokebunny", "A2", "A3", "A4", "B1", "B2", "B3", "B4"]), hash_replay(b"m1"))
    assert store.known_player("Pokebunny") is True
    assert store.known_player("NeverPlayed") is False


def test_link_change_count(store):
    assert store.change_count == 0
    store.link_player("disc123", "Pokebunny")
    assert store.change_count == 1
    store.link_player("disc999", "Pokebunny")  # rejected, no write
    assert store.change_count == 1
    store.unlink_player("disc123", "Pokebunny")
    assert store.change_count == 2
