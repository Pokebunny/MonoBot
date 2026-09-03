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
# Value-lost prior for Best Trader's ratio (see the spec below).
TRADE_PRIOR = 1000
MAX_AWARDS = 2


def trade_efficiency(player: MatchPlayer) -> float | None:
    """Kills per value lost, against a prior of TRADE_PRIOR lost. None when
    the stats weren't recorded.

    A raw ratio is unusable at the top of the scale — losing nothing divides
    by zero, and 3,150 kills for 50 lost is 63x — so this used to require
    1,000 value lost, which excluded the very games it should crown: a player
    who killed 13,900 and lost nothing wasn't in the running. The prior is
    added to EVERY player's losses instead of being a floor under a few, so a
    flawless game beats a smaller efficient one (10,000 for nothing = 10.0x,
    5,000 for 400 = 3.6x) and a player who barely fought scores near zero
    rather than infinity (200 for nothing = 0.2x). No eligibility gate needed.

    Lives here, and is imported rather than reimplemented, because the Best
    Trader award and the Highway Robbery achievement both read it. They were
    once two formulas over the same two numbers, and duly disagreed: 13,650
    destroyed for 2,100 lost is 6.5x raw but 4.4x with the prior, so a 5x
    badge got announced on a line reading 4.4x. One function, one answer.
    """
    if player.resources_killed is None or player.resources_lost is None:
        return None
    return player.resources_killed / (player.resources_lost + TRADE_PRIOR)


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
        trade_efficiency,
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
    # No Banker award: a big unspent bank is a mistake, not a highlight. The
    # Trust Fund achievement reframes it as a "won despite hoarding" surprise.
]


class Award(NamedTuple):
    key: str
    emoji: str
    title: str
    player: MatchPlayer
    detail: str
    z: float


# Game-level awards (no single player): winner's worst kill-value deficit
# for a Comeback, and lead flips for a Back-and-forth game.
COMEBACK_MIN_DEFICIT = 8000
LEAD_CHANGES_MIN = 5

# MVP line garnish: outkilling the rest of your own team COMBINED. Not a
# separate award — it overlaps MVP almost always — just a rare embellishment.
_OUTKILL_FLOOR = 10000


def mvp_outkilled_team(match: MonobattleMatch, mvp: MatchPlayer) -> bool:
    teammates = [p for p in match.team(mvp.team) if p is not mvp]
    if not teammates or any(p.resources_killed is None for p in teammates) or mvp.resources_killed is None:
        return False
    rest = sum(p.resources_killed for p in teammates)
    return mvp.resources_killed >= _OUTKILL_FLOOR and rest >= 1000 and mvp.resources_killed > rest


class GameAward(NamedTuple):
    key: str
    emoji: str
    title: str
    detail: str


def game_awards(match: MonobattleMatch) -> list[GameAward]:
    """Awards about the game itself rather than one player."""
    out = []
    if (
        match.winning_team is not None
        and match.comeback_deficit is not None
        and match.comeback_deficit >= COMEBACK_MIN_DEFICIT
    ):
        out.append(
            GameAward(
                "comeback", "🔄", "Comeback", f"Team {match.winning_team} won from {match.comeback_deficit:,} behind"
            )
        )
    if match.lead_changes is not None and match.lead_changes >= LEAD_CHANGES_MIN:
        out.append(GameAward("rollercoaster", "🎢", "Back-and-forth", f"the lead changed {match.lead_changes} times"))
    return out


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
