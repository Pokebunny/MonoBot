"""Per-match stat awards, given selectively.

Rather than always crowning a leader for every stat, an award is only given
when the leader is an OUTLIER within their own game — measured as z-score
against the lobby's distribution of that stat — so awards mark performances
that were actually unusual. Each match shows at most MAX_AWARDS, picking the
most extreme; an absolute floor per stat keeps trivial games quiet.
"""

import statistics
from typing import Callable, NamedTuple

from models.replay import MatchPlayer, MonobattleMatch

# Leader must be at least this many standard deviations above the lobby mean.
MIN_Z_SCORE = 1.5
MAX_AWARDS = 2


class AwardSpec(NamedTuple):
    key: str  # stable id, used for career aggregation
    emoji: str
    title: str
    value: Callable[[MatchPlayer], float | None]
    floor: float  # leader's raw value must reach this to qualify
    describe: Callable[[float], str]


SPECS = [
    AwardSpec(
        "worker_slayer",
        "🔪",
        "Worker Slayer",
        lambda p: p.econ_killed,
        1500,
        lambda v: f"{v:,.0f} economy destroyed",
    ),
    AwardSpec(
        "demolition",
        "🏗️",
        "Demolition",
        lambda p: p.tech_killed,
        1500,
        lambda v: f"{v:,.0f} tech destroyed",
    ),
    AwardSpec(
        "best_trader",
        "⚖️",
        "Best Trader",
        lambda p: (
            (p.resources_killed / p.resources_lost)
            if p.resources_killed and p.resources_lost and p.resources_lost >= 1000
            else None
        ),
        2.0,
        lambda v: f"{v:.1f}x trade efficiency",
    ),
    AwardSpec(
        "martyr",
        "🩸",
        "The Martyr",
        lambda p: p.resources_lost,
        8000,
        lambda v: f"{v:,.0f} value lost",
    ),
    AwardSpec(
        "banker",
        "💰",
        "The Banker",
        lambda p: p.resources_floated,
        8000,  # someone always floats *relatively* more; only true hoards count
        lambda v: f"{v:,.0f} resources unspent",
    ),
]


class Award(NamedTuple):
    key: str
    emoji: str
    title: str
    player: MatchPlayer
    detail: str
    z: float


def match_awards(match: MonobattleMatch, limit: int = MAX_AWARDS) -> list[Award]:
    """The most anomalous stat lines of a match, most extreme first."""
    candidates = []
    for spec in SPECS:
        scored = [(p, spec.value(p)) for p in match.players]
        scored = [(p, v) for p, v in scored if v is not None]
        if len(scored) < 4:  # too few measured players to call anything typical
            continue
        values = [v for _p, v in scored]
        leader, top = max(scored, key=lambda pv: pv[1])
        if top < spec.floor:
            continue
        spread = statistics.pstdev(values)
        if spread == 0:
            continue
        z = (top - statistics.fmean(values)) / spread
        if z < MIN_Z_SCORE:
            continue
        candidates.append(Award(spec.key, spec.emoji, spec.title, leader, spec.describe(top), z))
    candidates.sort(key=lambda a: a.z, reverse=True)
    return candidates[:limit]
