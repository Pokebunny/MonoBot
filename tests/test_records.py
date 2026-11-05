import datetime

from conftest import make_match, make_player
from services import match_embeds, records
from services.storage import MatchStore

BASE = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)


def _match(day, battle, comeback=None, swings=None, winner=1, kills=(1000,) * 8):
    players = [
        make_player(f"p{i}", 1 if i < 4 else 2, handle=f"h{i}", resources_killed=kills[i], resources_lost=1000)
        for i in range(8)
    ]
    return make_match(
        players,
        winning_team=winner,
        file_name=f"{day}.SC2Replay",
        map_name="Monobattle",
        played_at=BASE + datetime.timedelta(days=day),
        duration_seconds=battle + 50,
        pick_phase_seconds=70,  # game seconds: 50 real
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


def test_kills_rank_single_game_performances():
    matches = [(1, _match(1, 900, kills=(50000, *(1000,) * 7)))]
    top = _board(records.record_boards(matches), "kills").rows[0]
    assert (top.value, top.who) == ("50,000", "p0 (Zergling)")


def test_every_game_award_has_a_board_measuring_its_stat():
    from services.awards import SPECS

    match = _match(1, 900)
    match.players[2].econ_killed, match.players[5].tech_killed = 9000, 7000
    match.players[6].resources_lost = 40000
    boards = records.record_boards([(1, match)])
    for spec in SPECS:
        board = _board(boards, "trades" if spec.key == "best_trader" else spec.key)
        assert board.rows, spec.key
    assert _board(boards, "worker_slayer").rows[0][:2] == ("9,000", "p2 (Zergling)")
    assert _board(boards, "demolition").rows[0][:2] == ("7,000", "p5 (Zergling)")
    assert _board(boards, "martyr").rows[0][:2] == ("40,000", "p6 (Zergling)")


def test_category_words_open_their_page():
    keys = list(records.RECORD_ALIASES)
    # Pages open by position, so the two lists must agree.
    assert keys == [b.key for b in records.record_boards([])]
    assert records.board_index("Longest") == keys.index("longest")
    assert records.board_index("lead") == keys.index("swings")
    assert records.board_index("workers") == keys.index("worker_slayer")
    assert records.board_index("worker_slayer") == keys.index("worker_slayer")
    assert records.board_index("tech") == keys.index("demolition")
    assert records.board_index("martyr") == keys.index("martyr")
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
