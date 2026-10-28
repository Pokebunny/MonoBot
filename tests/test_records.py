import datetime

from models.replay import MatchPlayer, MonobattleMatch
from services import match_embeds, records
from services.storage import MatchStore

BASE = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)


def _match(day, battle, comeback=None, swings=None, winner=1, kills=(1000,) * 8):
    players = [
        MatchPlayer(name=f"p{i}", toon_handle=f"h{i}", team=1 if i < 4 else 2, race="Zerg", pick="Zergling",
                    resources_killed=kills[i], resources_lost=1000, unit_counts={})
        for i in range(8)
    ]  # fmt: skip
    return MonobattleMatch(
        file_name=f"{day}.SC2Replay",
        map_name="Monobattle",
        played_at=BASE + datetime.timedelta(days=day),
        duration_seconds=battle + 60,
        game_type="4v4",
        pick_mode="blind_random",
        pick_phase_seconds=60,
        players=players,
        winning_team=winner,
        winner_confidence=1.0 if winner else 0.0,
        winner_method="recorded" if winner else "unknown",
        comeback_deficit=comeback,
        lead_changes=swings,
    )


def _board(boards, key):
    return next(b for b in boards if b.key == key)


def test_comebacks_rank_biggest_first_and_name_the_winners():
    matches = [(1, _match(1, 900, comeback=9000)), (2, _match(2, 900, comeback=30000, winner=2))]
    rows = _board(records.record_boards(matches), "comebacks").rows
    assert [(r.match_id, r.value) for r in rows] == [(2, "30,000 behind"), (1, "9,000 behind")]
    assert rows[0].who == "p4, p5, p6, p7"


def test_fastest_wins_skip_undecided_games():
    matches = [(1, _match(1, 200, winner=None)), (2, _match(2, 300)), (3, _match(3, 900))]
    rows = _board(records.record_boards(matches), "fastest").rows
    assert [r.match_id for r in rows] == [2, 3]
    assert rows[0].value == "5:00"


def test_kills_rank_single_game_performances():
    matches = [(1, _match(1, 900, kills=(50000, *(1000,) * 7)))]
    top = _board(records.record_boards(matches), "kills").rows[0]
    assert (top.value, top.who) == ("50,000", "p0 (Zergling)")


def test_category_words_open_their_page():
    keys = list(records.RECORD_ALIASES)
    assert records.board_index("Longest") == keys.index("longest")
    assert records.board_index("lead") == keys.index("swings")
    assert records.board_index("nonsense") is None


def test_embed_points_to_last():
    board = _board(records.record_boards([(7, _match(1, 900, comeback=12000))]), "comebacks")
    embed = match_embeds.record_board(board, 0, 6, {7: 1})
    assert "#1" in embed.description and "!last" in embed.footer.text


def test_match_numbers_follow_play_order(tmp_path):
    store = MatchStore(str(tmp_path / "m.db"))
    # Uploaded out of order: the later game first.
    late = store.ingest(_match(3, 900), "a").match_id
    early = store.ingest(_match(1, 900), "b").match_id
    assert store.match_numbers() == {early: 1, late: 2}
    assert [i for i, _ in store.all_matches()] == [early, late]
