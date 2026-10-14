"""The rush rules the parser applies to every player: cannons for Protoss,
spine/spore crawlers for Zerg."""

from services.replay_parser import (
    _CANNON_RUSH_WINDOW_SECONDS,
    _CRAWLER_RUSH_WINDOW_SECONDS,
    _cannon_rushed,
    _PlayerTally,
    _spine_rushed,
)


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


def test_spine_rush_is_an_early_crawler_well_toward_an_enemy_base():
    start = 60
    tally = _PlayerTally()
    tally.home = (0.0, 0.0)
    enemies = [(100.0, 0.0)]
    tally.crawler_starts = [(start + 200, 55.0, 0.0)]  # just past halfway: a mid-map base's spore
    assert _spine_rushed(tally, enemies, start) is False
    tally.crawler_starts.append((start + _CRAWLER_RUSH_WINDOW_SECONDS + 1, 90.0, 0.0))  # forward, but late
    assert _spine_rushed(tally, enemies, start) is False
    tally.crawler_starts.append((start + 300, 80.0, 0.0))  # at the proxy hatch, in time
    assert _spine_rushed(tally, enemies, start) is True
    assert _cannon_rushed(tally, enemies, start) is False  # crawlers aren't cannons
