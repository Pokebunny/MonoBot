"""Unit tests for _infer_winner on synthetic replays (both team directions)."""

import types

from services.replay_parser import _infer_winner

ChatEvent = type("ChatEvent", (), {})
PlayerLeaveEvent = type("PlayerLeaveEvent", (), {})


def _player(name):
    return types.SimpleNamespace(name=name)


def _chat(second, name, text):
    e = ChatEvent()
    e.second, e.player, e.text = second, _player(name), text
    return e


def _leave(second, name):
    e = PlayerLeaveEvent()
    e.second, e.player = second, _player(name)
    return e


def _replay(events, seconds=900):
    team1 = types.SimpleNamespace(number=1, players=[_player(f"A{i}") for i in range(4)])
    team2 = types.SimpleNamespace(number=2, players=[_player(f"B{i}") for i in range(4)])
    # Recording-end marker: inference reads the end time from the last event.
    events = events + [_chat(seconds, "B3", "...")]
    return types.SimpleNamespace(
        winner=None,
        teams=[team1, team2],
        events=events,
        game_length=types.SimpleNamespace(seconds=seconds),
    )


def test_gg_concession_team1_loses():
    replay = _replay([_chat(850, "A0", "gg"), _leave(860, "A0"), _leave(880, "A1")])
    team, conf, method = _infer_winner(replay)
    assert team == 2
    assert conf >= 0.7


def test_gg_concession_team2_loses():
    replay = _replay([_chat(850, "B0", "gg"), _leave(860, "B0"), _leave(880, "B1")])
    team, conf, method = _infer_winner(replay)
    assert team == 1
    assert conf >= 0.7


def test_more_departures_marks_the_loser():
    # Team 1 lost two players, team 2 one -> team 1 conceded.
    replay = _replay([_leave(300, "A0"), _leave(400, "A1"), _leave(500, "B0")], seconds=900)
    team, conf, method = _infer_winner(replay)
    assert team == 2
    assert conf >= 0.7
    assert "departures" in method


def test_recorder_ending_leave_excluded():
    # The final leave near the end is the recorder's; only A0's counts.
    replay = _replay([_leave(300, "A0"), _leave(895, "B0")], seconds=900)
    team, conf, method = _infer_winner(replay)
    assert team == 2


def test_balanced_departures_no_signal():
    # Equal departures on both teams -> net-departures stays silent.
    replay = _replay([_leave(300, "A0"), _leave(400, "B0")], seconds=900)
    team, conf, method = _infer_winner(replay)
    assert team is None


def test_single_late_leaver_confident():
    replay = _replay([_leave(880, "B2")], seconds=900)
    team, conf, method = _infer_winner(replay)
    assert team == 1
    assert conf >= 0.7
