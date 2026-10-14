"""The cannon-rush rule the parser applies to every Protoss player."""

from services.replay_parser import _CANNON_RUSH_WINDOW_SECONDS, _cannon_rushed, _PlayerTally


def test_cannon_rush_is_an_early_cannon_nearer_an_enemy_base():

    start = 60
    tally = _PlayerTally()
    tally.home = (10.0, 10.0)
    enemies = [(150.0, 150.0)]
    assert _cannon_rushed(tally, enemies, start) is False  # no cannons at all
    tally.cannon_starts = [(start + 100, 20.0, 20.0)]  # at home: defense
    assert _cannon_rushed(tally, enemies, start) is False
    tally.cannon_starts.append((start + _CANNON_RUSH_WINDOW_SECONDS + 1, 140.0, 140.0))  # forward, but late
    assert _cannon_rushed(tally, enemies, start) is False
    tally.cannon_starts.append((start + 150, 140.0, 140.0))  # forward and early
    assert _cannon_rushed(tally, enemies, start) is True
    tally.home = None
    assert _cannon_rushed(tally, enemies, start) is None  # nothing to measure from
