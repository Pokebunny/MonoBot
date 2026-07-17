"""Form the most balanced teams from a queue of rated players.

A monobattle is 2k players split into two teams of k. The number of distinct
splits is small (35 for 4v4, 10 for 3v3), so we enumerate them all and pick
the one whose predicted win probability is closest to 50/50. Team-strength
prediction goes through services.rating so openskill stays isolated there.
"""

import itertools

from models.matchmaking import ProposedMatch, QueuedPlayer
from services.rating import predict_win_probability


def _win_probability(team1: list[QueuedPlayer], team2: list[QueuedPlayer]) -> float:
    return predict_win_probability(
        [(p.mu, p.sigma) for p in team1],
        [(p.mu, p.sigma) for p in team2],
    )


def balance_teams(players: list[QueuedPlayer]) -> ProposedMatch:
    """Split an even number of players into the two most balanced teams.

    Fixing the first player on team1 dedupes mirror-image splits (team1/team2
    swaps), so 8 players yield 35 candidates rather than 70."""
    n = len(players)
    if n < 2 or n % 2 != 0:
        raise ValueError(f"need an even number of players (>=2), got {n}")
    half = n // 2

    anchor, rest = players[0], players[1:]
    best: ProposedMatch | None = None
    best_gap = 2.0
    for combo in itertools.combinations(range(len(rest)), half - 1):
        team1 = [anchor] + [rest[i] for i in combo]
        team2 = [rest[i] for i in range(len(rest)) if i not in combo]
        p1 = _win_probability(team1, team2)
        gap = abs(0.5 - p1)
        if gap < best_gap:
            best_gap = gap
            best = ProposedMatch(team1=team1, team2=team2, team1_win_probability=p1)
    assert best is not None  # guaranteed since at least one combo exists
    return best
