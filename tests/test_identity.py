import datetime

import pytest
from models.replay import MatchPlayer, MonobattleMatch
from services.storage import MatchStore, hash_replay


@pytest.fixture
def store(tmp_path):
    s = MatchStore(str(tmp_path / "test.db"))
    yield s
    s.close()


def _match(names, winning_team=None, handles=None):
    # names may be display names; handles overrides the toon_handle per slot
    # (defaults to "H-<name>") so we can model two accounts sharing a name.
    handles = handles or [f"H-{n}" for n in names]
    players = [
        MatchPlayer(name=n, toon_handle=h, team=(1 if i < 4 else 2), race="Zerg", pick="Zergling", unit_counts={})
        for i, (n, h) in enumerate(zip(names, handles))
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
    assert store.link_player("disc123", "Pokebunny").status == "linked"
    assert store.sc2_names_for("disc123") == ["Pokebunny"]
    assert store.discord_id_for("Pokebunny") == "disc123"


def test_link_multiple_names(store):
    store.link_player("disc123", "Pokebunny")
    store.link_player("disc123", "Kuschelbunny")
    assert store.sc2_names_for("disc123") == ["Kuschelbunny", "Pokebunny"]


def test_name_claimed_by_other_is_rejected(store):
    store.link_player("disc123", "Pokebunny")
    result = store.link_player("disc999", "Pokebunny")
    assert result.status == "taken"
    assert result.owner == "disc123"
    assert store.discord_id_for("Pokebunny") == "disc123"


def test_reclaim_own_name_is_noop_success(store):
    assert store.link_player("disc123", "Pokebunny").status == "linked"
    assert store.link_player("disc123", "Pokebunny").status == "linked"
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


# --- handle binding: names claimed pre-play, handles bound on first game ---

_ROSTER = ["Pokebunny", "A2", "A3", "A4", "B1", "B2", "B3", "B4"]


def test_link_is_case_insensitive(store):
    store.ingest(_match(_ROSTER, winning_team=1), hash_replay(b"g1"))  # has "Pokebunny"
    result = store.link_player("disc123", "pokebunny")  # typed lowercase
    assert result.status == "linked"
    assert result.handle == "H-Pokebunny"
    assert store.handles_for("disc123") == ["H-Pokebunny"]


def test_case_variant_name_is_same_claim(store):
    assert store.link_player("disc123", "Pokebunny").status == "linked"
    # Another user can't claim a different casing of the same name.
    assert store.link_player("disc999", "POKEBUNNY").status == "taken"
    # The owner re-linking any casing is a no-op success.
    assert store.link_player("disc123", "pokeBunny").status == "linked"


def test_handle_unbound_until_played(store):
    store.link_player("disc123", "Pokebunny")
    # Linked but never played: a name claim, no bound handle yet.
    assert store.sc2_names_for("disc123") == ["Pokebunny"]
    assert store.handles_for("disc123") == []


def test_link_binds_from_existing_history(store):
    # The common case: player already has games, then links.
    store.ingest(_match(_ROSTER, winning_team=1), hash_replay(b"g1"))
    result = store.link_player("disc123", "Pokebunny")
    assert result.status == "linked"
    assert result.handle == "H-Pokebunny"
    assert store.handles_for("disc123") == ["H-Pokebunny"]


def test_handle_bound_on_first_game_after_linking(store):
    # Linked before any history, then plays: binds on ingest.
    store.link_player("disc123", "Pokebunny")
    assert store.handles_for("disc123") == []
    store.ingest(_match(_ROSTER, winning_team=1), hash_replay(b"g1"))
    assert store.handles_for("disc123") == ["H-Pokebunny"]


def test_ambiguous_name_not_bound(store):
    # Two "Rain" accounts have played (in different games).
    r1 = _match(
        ["Rain", "A2", "A3", "A4", "B1", "B2", "B3", "B4"],
        winning_team=1,
        handles=["H-rain1", "H-A2", "H-A3", "H-A4", "H-B1", "H-B2", "H-B3", "H-B4"],
    )
    r2 = _match(
        ["Rain", "C2", "C3", "C4", "D1", "D2", "D3", "D4"],
        winning_team=1,
        handles=["H-rain2", "H-C2", "H-C3", "H-C4", "H-D1", "H-D2", "H-D3", "H-D4"],
    )
    r2 = r2.model_copy(update={"played_at": r2.played_at + datetime.timedelta(minutes=30)})
    store.ingest(r1, hash_replay(b"r1"))
    store.ingest(r2, hash_replay(b"r2"))
    result = store.link_player("disc123", "Rain")
    assert result.status == "ambiguous"
    assert result.candidates == 2
    assert store.handles_for("disc123") == []  # not auto-bound


def test_aliases_and_old_name_resolution(store):
    # One account (H-x) plays under two names across two games.
    rest1 = ["A2", "A3", "A4", "B1", "B2", "B3", "B4"]
    rest2 = ["C2", "C3", "C4", "D1", "D2", "D3", "D4"]
    g1 = _match(["OldName"] + rest1, winning_team=1, handles=["H-x"] + [f"H-{n}" for n in rest1])
    g2 = _match(["NewName"] + rest2, winning_team=1, handles=["H-x"] + [f"H-{n}" for n in rest2])
    g2 = g2.model_copy(update={"played_at": g2.played_at + datetime.timedelta(minutes=30)})
    store.ingest(g1, hash_replay(b"g1"))
    store.ingest(g2, hash_replay(b"g2"))

    # A former name still resolves to the account (and case-insensitively).
    assert store.handles_for_name("OldName") == ["H-x"]
    assert store.handles_for_name("newname") == ["H-x"]
    # Aliases are listed most-recent first.
    assert store.aliases_for_handle("H-x") == ["NewName", "OldName"]
    # Linking by the old name binds to the same account.
    assert store.link_player("disc1", "OldName").handle == "H-x"


def test_candidates_and_bind_specific(store):
    r1 = _match(
        ["Rain", "A2", "A3", "A4", "B1", "B2", "B3", "B4"],
        winning_team=1,
        handles=["H-rain1", "H-A2", "H-A3", "H-A4", "H-B1", "H-B2", "H-B3", "H-B4"],
    )
    r2 = _match(
        ["Rain", "C2", "C3", "C4", "D1", "D2", "D3", "D4"],
        winning_team=1,
        handles=["H-rain2", "H-C2", "H-C3", "H-C4", "H-D1", "H-D2", "H-D3", "H-D4"],
    )
    r2 = r2.model_copy(update={"played_at": r2.played_at + datetime.timedelta(minutes=30)})
    store.ingest(r1, hash_replay(b"r1"))
    store.ingest(r2, hash_replay(b"r2"))

    cands = store.candidates_for_name("rain")  # case-insensitive
    assert {c[0] for c in cands} == {"H-rain1", "H-rain2"}
    # pick one account explicitly
    assert store.bind_specific("disc1", "Rain", "H-rain2")
    assert store.handles_for("disc1") == ["H-rain2"]
    # another user can't claim the same name
    assert not store.bind_specific("disc2", "Rain", "H-rain1")


def test_add_account_merges_same_named_accounts(store):
    # Cross-region: two accounts share the display name "Rain".
    r1 = _match(
        ["Rain", "A2", "A3", "A4", "B1", "B2", "B3", "B4"],
        winning_team=1,
        handles=["H-rain1", "H-A2", "H-A3", "H-A4", "H-B1", "H-B2", "H-B3", "H-B4"],
    )
    r2 = _match(
        ["Rain", "C2", "C3", "C4", "D1", "D2", "D3", "D4"],
        winning_team=1,
        handles=["H-rain2", "H-C2", "H-C3", "H-C4", "H-D1", "H-D2", "H-D3", "H-D4"],
    )
    r2 = r2.model_copy(update={"played_at": r2.played_at + datetime.timedelta(minutes=30)})
    store.ingest(r1, hash_replay(b"r1"))
    store.ingest(r2, hash_replay(b"r2"))
    store.bind_specific("disc1", "Rain", "H-rain1")  # first account via picker
    assert store.add_account("disc1", "H-rain2")  # second, same name, via !addaccount
    assert set(store.handles_for("disc1")) == {"H-rain1", "H-rain2"}
    assert store.merge_map()["H-rain1"] == store.merge_map()["H-rain2"]
    assert not store.add_account("disc2", "H-rain2")  # can't grab someone's account


def test_linked_accounts_merge_into_one_rating(store):
    from services.rating import RatingBook

    # One person plays two accounts (different names) in two games.
    g1 = _match(
        ["MainAcct", "A2", "A3", "A4", "B1", "B2", "B3", "B4"],
        winning_team=1,
        handles=["H-main", "H-A2", "H-A3", "H-A4", "H-B1", "H-B2", "H-B3", "H-B4"],
    )
    g2 = _match(
        ["SmurfAcct", "C2", "C3", "C4", "D1", "D2", "D3", "D4"],
        winning_team=1,
        handles=["H-smurf", "H-C2", "H-C3", "H-C4", "H-D1", "H-D2", "H-D3", "H-D4"],
    )
    g2 = g2.model_copy(update={"played_at": g2.played_at + datetime.timedelta(minutes=30)})
    store.ingest(g1, hash_replay(b"g1"))
    store.ingest(g2, hash_replay(b"g2"))
    store.link_player("disc1", "MainAcct")
    store.link_player("disc1", "SmurfAcct")
    assert set(store.handles_for("disc1")) == {"H-main", "H-smurf"}

    book = RatingBook.from_matches((m for _, m in store.all_matches()), store.merge_map())
    # both accounts' games fall under a single rating
    assert book.rating_for("H-main") is book.rating_for("H-smurf")
    assert book.rating_for("H-main").wins == 2


def test_same_name_different_accounts_dont_merge(store):
    from services.rating import RatingBook

    # Two different "Rain" accounts, each in their own game.
    g1 = _match(["Rain"] + _ROSTER[1:], winning_team=1, handles=["H-rain1"] + [f"H-{n}" for n in _ROSTER[1:]])
    g2_names = ["Rain", "C2", "C3", "C4", "D1", "D2", "D3", "D4"]
    g2 = _match(g2_names, winning_team=2, handles=["H-rain2"] + [f"H-{n}" for n in g2_names[1:]])
    g2 = g2.model_copy(update={"played_at": g1.played_at + datetime.timedelta(minutes=30)})
    store.ingest(g1, hash_replay(b"g1"))
    store.ingest(g2, hash_replay(b"g2"))
    book = RatingBook.from_matches(m for _, m in store.all_matches())
    # Separate ratings despite the shared display name.
    assert "H-rain1" in book.ratings and "H-rain2" in book.ratings
    assert book.ratings["H-rain1"].wins == 1 and book.ratings["H-rain1"].losses == 0
    assert book.ratings["H-rain2"].wins == 0 and book.ratings["H-rain2"].losses == 1
