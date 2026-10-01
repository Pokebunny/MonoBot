import datetime
import zoneinfo

from models.replay import MatchPlayer, MonobattleMatch
from services import match_embeds
from services.rating import match_rating_deltas
from services.recap import SESSION_GAP, build_recap, latest_session
from services.storage import MatchStore

BASE = datetime.datetime(2026, 9, 30, 23, 0, tzinfo=datetime.UTC)


def _match(winning_team, minutes=0, team1=None, team2=None, kills=None, duration=900):
    team1 = team1 or ["A1", "A2", "A3", "A4"]
    team2 = team2 or ["B1", "B2", "B3", "B4"]
    kills = kills or {}
    players = [
        MatchPlayer(
            name=n, toon_handle=n, team=t, race="Zerg", pick="Zergling", unit_counts={},
            resources_killed=kills.get(n, 100),
        )
        for t, names in ((1, team1), (2, team2))
        for n in names
    ]  # fmt: skip
    return MonobattleMatch(
        file_name=f"{minutes}.SC2Replay",
        map_name="Monobattle LotV - Map Rotation",
        played_at=BASE + datetime.timedelta(minutes=minutes),
        duration_seconds=duration,
        game_type="4v4",
        pick_mode="blind_random",
        pick_phase_seconds=60,
        players=players,
        winning_team=winning_team,
        winner_confidence=1.0,
        winner_method="recorded",
    )


def _numbered(matches):
    return list(enumerate(matches, 1))


def test_latest_session_stops_at_a_long_gap():
    gap = int(SESSION_GAP.total_seconds() // 60)
    history = _numbered([_match(1, 0), _match(1, 30), _match(1, 30 + gap + 1), _match(1, 60 + gap + 1)])
    assert [mid for mid, _ in latest_session(history)] == [3, 4]


def test_latest_session_keeps_a_gap_at_the_limit():
    gap = int(SESSION_GAP.total_seconds() // 60)
    history = _numbered([_match(1, 0), _match(1, gap)])
    assert len(latest_session(history)) == 2


def test_record_and_mvps():
    history = _numbered([_match(1, 0, kills={"B1": 5000}), _match(2, 30, kills={"B1": 5000}), _match(1, 60)])
    recap = build_recap(history, history, {}, [])
    by = {p.handle: p for p in recap.players}
    assert recap.games == 3
    assert (by["A1"].wins, by["A1"].losses) == (2, 1)
    assert by["B1"].mvps == 2


def test_rating_change_matches_the_per_game_deltas():
    history = _numbered([_match(1, 0), _match(2, 30), _match(1, 60), _match(1, 90)])
    session = history[1:]  # first game is from an earlier session
    recap = build_recap(session, history, {}, [])
    a1 = next(p for p in recap.players if p.handle == "A1")
    before = match_rating_deltas(history, 2)["A1"][0]
    after = match_rating_deltas(history, 4)["A1"][1]
    assert (a1.rating_before, a1.rating_after) == (before, after)


def test_unrated_games_count_but_do_not_move_rating():
    three_v_three = _match(1, 0, team1=["A1", "A2", "A3"], team2=["B1", "B2", "B3"])
    recap = build_recap([(1, three_v_three)], [(1, three_v_three)], {}, [])
    a1 = next(p for p in recap.players if p.handle == "A1")
    assert a1.wins == 1 and a1.rating_change is None


def test_merged_accounts_are_one_line():
    history = _numbered([_match(1, 0), _match(1, 30, team1=["Alt", "A2", "A3", "A4"])])
    recap = build_recap(history, history, {"Alt": "A1"}, [])
    a1 = next(p for p in recap.players if p.handle == "A1")
    assert a1.wins == 2
    assert all(p.handle != "Alt" for p in recap.players)


def test_undecided_session_has_nothing_to_report():
    assert build_recap([(1, _match(None))], [(1, _match(None))], {}, []) is None


def test_unlocks_between_uses_the_games_window(tmp_path):
    store = MatchStore(str(tmp_path / "t.db"))
    store.record_unlocks([
        ("A1", "long_haul", (BASE - datetime.timedelta(days=1)).replace(tzinfo=None).isoformat()),
        ("A2", "long_haul", BASE.replace(tzinfo=None).isoformat()),
    ])  # fmt: skip
    assert store.unlocks_between(BASE, BASE + datetime.timedelta(hours=2)) == [("A2", "long_haul")]


def test_embed_lists_everyone_and_their_achievements():
    history = _numbered([_match(1, 0), _match(1, 30)])
    recap = build_recap(history, history, {}, [("A1", "long_haul")])
    embed = match_embeds.session_recap(recap, {"A1": "Alice"}, zoneinfo.ZoneInfo("America/New_York"), "Recap")
    assert "**Alice** 2-0" in embed.description
    assert "The Long Haul" in embed.description
    player_lines = [line for line in embed.description.splitlines() if " 2-0" in line or " 0-2" in line]
    assert len(player_lines) == 8
