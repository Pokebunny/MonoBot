"""Balanced team splits: few enough (35 for 4v4) to enumerate them all."""

import datetime as dt
import itertools

from models.matchmaking import ProposedMatch, QueuedPlayer
from services.rating import predict_win_probability


def _win_probability(team1: list[QueuedPlayer], team2: list[QueuedPlayer]) -> float:
    return predict_win_probability(
        [(p.mu, p.sigma) for p in team1],
        [(p.mu, p.sigma) for p in team2],
    )


def _as_shown(team1: list[QueuedPlayer], team2: list[QueuedPlayer], p1: float) -> ProposedMatch:
    """Each team strongest first, the strongest player's team as Team 1. By mu,
    not the board's mu - 3*sigma, which sinks anyone with few games."""

    def by_skill(team: list[QueuedPlayer]) -> list[QueuedPlayer]:
        return sorted(team, key=lambda p: p.mu, reverse=True)

    team1, team2 = by_skill(team1), by_skill(team2)
    if team2[0].mu > team1[0].mu:
        team1, team2, p1 = team2, team1, 1.0 - p1
    return ProposedMatch(team1=team1, team2=team2, team1_win_probability=p1)


def ranked_matches(players: list[QueuedPlayer], limit: int | None = None) -> list[ProposedMatch]:
    """Every distinct split of an even roster, most balanced first. Anchoring
    the first player dedupes mirror images (35 splits for 8, not 70)."""
    n = len(players)
    if n < 2 or n % 2 != 0:
        raise ValueError(f"need an even number of players (>=2), got {n}")
    half = n // 2

    anchor, rest = players[0], players[1:]
    matches = []
    for combo in itertools.combinations(range(len(rest)), half - 1):
        team1 = [anchor] + [rest[i] for i in combo]
        team2 = [rest[i] for i in range(len(rest)) if i not in combo]
        matches.append(_as_shown(team1, team2, _win_probability(team1, team2)))
    matches.sort(key=lambda m: abs(0.5 - m.team1_win_probability))
    return matches if limit is None else matches[:limit]


def balance_teams(players: list[QueuedPlayer]) -> ProposedMatch:
    return ranked_matches(players, limit=1)[0]


def expired(joined_at: dict[str, dt.datetime], now: dt.datetime, timeout: dt.timedelta) -> list[str]:
    """Queued players whose last Join press is at least `timeout` old."""
    return [uid for uid, since in joined_at.items() if now - since >= timeout]


def next_roster(roster: list[str], sitting_out: list[str], waitlist: list[str]) -> tuple[list[str], list[str], int]:
    """Everyone not sitting out keeps their spot; vacated spots go to the
    waitlist in join order. Returns (roster, promoted, spots still unfilled)."""
    kept = [uid for uid in roster if uid not in sitting_out]
    promoted = [uid for uid in waitlist if uid not in roster][: len(roster) - len(kept)]
    return kept + promoted, promoted, len(roster) - len(kept) - len(promoted)
