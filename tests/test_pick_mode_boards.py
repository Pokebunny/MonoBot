"""!leaderboard br / sd: a board rated on one pick mode's games only."""

import datetime

import pytest
from cogs.leaderboard import _find_mode
from conftest import BASE, make_match, make_player
from services import match_embeds
from services.rating import pick_board, season_book
from services.storage import MatchStore, hash_replay


def _match(index, pick_mode, winning_team=1):
    return make_match(
        _roster(),
        winning_team=winning_team,
        file_name=f"g{index}.SC2Replay",
        played_at=BASE + datetime.timedelta(minutes=20 * index),
        pick_mode=pick_mode,
        pick_phase_seconds=63 if pick_mode == "blind_random" else 200,
    )


def _roster():
    names = ["A0", "A1", "A2", "A3", "B0", "B1", "B2", "B3"]
    return [
        make_player(n, 1 if i < 4 else 2, handle=f"h-{n}", unit_counts={"Zergling": 100}) for i, n in enumerate(names)
    ]


@pytest.fixture
def store(tmp_path):
    store = MatchStore(str(tmp_path / "modes.db"))
    # Team 1 wins every blind random game, team 2 every single draft one.
    for i in range(3):
        store.ingest(_match(i, "blind_random", winning_team=1), hash_replay(f"br{i}".encode()))
    for i in range(3, 5):
        store.ingest(_match(i, "single_draft", winning_team=2), hash_replay(f"sd{i}".encode()))
    yield store
    store.close()


@pytest.mark.parametrize(
    "query, mode, rest",
    [
        ("br", "blind_random", ""),
        ("SD", "single_draft", ""),
        ("blind random zerg", "blind_random", "zerg"),
        ("5 Single Draft s1", "single_draft", "5 s1"),
        ("singledraft", "single_draft", ""),
        ("zerg 3", None, "zerg 3"),
    ],
)
def test_find_mode(query, mode, rest):
    assert _find_mode(query) == (mode, rest)


def test_mode_board_counts_only_that_modes_games(store):
    br = season_book(store, None, mode="blind_random").rating_for("h-A0")
    sd = season_book(store, None, mode="single_draft").rating_for("h-A0")
    assert (br.wins, br.losses) == (3, 0)
    assert (sd.wins, sd.losses) == (0, 2)
    every = season_book(store, None).rating_for("h-A0")
    assert (every.wins, every.losses) == (3, 2)


def test_mode_unit_board(store):
    board = pick_board(store, None, "Zergling", "single_draft")
    a0 = next(r for r in board if r.handle == "h-A0")
    assert (a0.wins, a0.losses) == (0, 2)


def test_mode_board_embed_names_the_mode():
    embed = match_embeds.leaderboard([], season="Season 3", mode="single_draft")
    assert embed.title == "Monobattle Leaderboard (Single Draft) — Season 3"
    assert "Single Draft" in embed.description
