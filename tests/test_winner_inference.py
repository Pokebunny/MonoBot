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


def test_early_dropout_ignored():
    # A leaver before 120s is a dropout, not a concession.
    replay = _replay([_leave(30, "A0")])
    team, conf, method = _infer_winner(replay)
    assert team is None
    assert method == "unknown"


def test_early_leaver_low_confidence():
    # First leaver long before the end: signal fires but below rating gate.
    replay = _replay([_leave(300, "A0")], seconds=900)
    team, conf, method = _infer_winner(replay)
    assert team == 2
    assert conf < 0.7


def test_late_first_leaver_confident():
    replay = _replay([_leave(880, "B2")], seconds=900)
    team, conf, method = _infer_winner(replay)
    assert team == 1
    assert conf >= 0.7
