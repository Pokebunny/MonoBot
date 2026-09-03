"""Guards against a stat being DISPLAYED one way and GATED another.

This has bitten twice. Best Trader was smoothed with a prior while Highway
Robbery kept a raw killed/lost, so a 5x badge got announced on a line reading
4.4x. Before that, the match clock counted the draft while the achievements
did not.

Two layers here:

1. `test_trade_efficiency_has_one_implementation` — the award and the
   achievement must call the SAME function object, not two expressions that
   happen to agree today.
2. `PAIRED_METRICS` — every award number that a player can read off an embed,
   next to the achievement threshold over the same quantity. The test asserts
   they agree about crossing the bar across the whole range, boundaries
   included. ADD A ROW HERE whenever a new achievement gates on something an
   award prints; that is the whole point of the file.
"""

import datetime

import pytest
from models.replay import MatchPlayer, MonobattleMatch
from services import awards
from services.achievements import SPECS_BY_KEY
from services.achievements.core import Tally, _MatchContext
from services.awards import trade_efficiency

AWARDS = {s.key: s for s in awards.SPECS}


def _player(killed=None, lost=None, econ=None, tech=None):
    return MatchPlayer(
        name="A1",
        toon_handle="A1",
        team=1,
        race="Zerg",
        pick="Zergling",
        resources_killed=killed,
        resources_lost=lost,
        econ_killed=econ,
        tech_killed=tech,
        unit_counts={},
    )


def _match(players):
    return MonobattleMatch(
        file_name="t.SC2Replay",
        map_name="Monobattle LotV - Map Rotation",
        played_at=datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone.utc),
        duration_seconds=900,
        game_type="4v4",
        pick_mode="blind_random",
        pick_phase_seconds=60,
        players=players,
        winning_team=1,
        winner_confidence=1.0,
        winner_method="recorded",
    )


def test_trade_efficiency_has_one_implementation():
    """The award spec must hold the shared function itself. Re-implementing it
    is exactly how the 5x-badge-on-a-4.4x-line bug happened."""
    assert AWARDS["best_trader"].value is trade_efficiency
    assert awards.trade_efficiency is trade_efficiency


def test_the_game_that_exposed_the_split():
    """Match #1394: 13,650 destroyed for 2,100 lost. 6.5x as a raw ratio,
    4.4x with the prior. The badge asks for 5x, so it must NOT fire."""
    p = _player(killed=13650, lost=2100)
    shown = AWARDS["best_trader"].value(p)
    assert shown == pytest.approx(4.4, abs=0.05)
    tally = Tally()
    tally.update(p, _MatchContext(_match([p]), None, None, {}, {}))
    assert tally.best_trade == shown  # badge is judged on the number shown
    assert not SPECS_BY_KEY["highway_robbery"].check(_history(tally))


def _history(live: Tally):
    from services.achievements.core import PlayerHistory

    h = PlayerHistory()
    h.live = live
    return h


# (achievement key, bar it claims, the field the award prints, award key)
PAIRED_METRICS = [
    ("economic_crash", 10000, "econ_killed", "worker_slayer"),
    ("scorched_earth", 8000, "tech_killed", "demolition"),
    ("pyrrhic_victory", 30000, "resources_lost", "martyr"),
]


@pytest.mark.parametrize("key, bar, field, award_key", PAIRED_METRICS)
@pytest.mark.parametrize("scale", [0.0, 0.5, 0.999, 1.0, 1.001, 2.0])
def test_award_line_agrees_with_the_badge_about_the_bar(key, bar, field, award_key, scale):
    """Whatever the embed prints for this stat is the number the badge is
    judged on, on both sides of the threshold."""
    value = int(bar * scale)
    p = _player(**{{"econ_killed": "econ", "tech_killed": "tech", "resources_lost": "lost"}[field]: value})
    printed = AWARDS[award_key].value(p)
    assert printed == value
    assert (printed >= bar) == (value >= bar)


@pytest.mark.parametrize(
    "killed, lost",
    [(13650, 2100), (10000, 0), (200, 0), (5000, 400), (25050, 3955), (0, 5000), (30000, 2000)],
)
def test_trade_award_and_badge_never_disagree(killed, lost):
    p = _player(killed=killed, lost=lost)
    tally = Tally()
    tally.update(p, _MatchContext(_match([p]), None, None, {}, {}))
    assert tally.best_trade == AWARDS["best_trader"].value(p)


def test_trade_efficiency_is_none_without_stats():
    assert trade_efficiency(_player(killed=None, lost=100)) is None
    assert trade_efficiency(_player(killed=100, lost=None)) is None
