"""Choose soft season-reset parameters by backtest.

A soft reset seeds each player at the boundary from their career rating:
    mu    = DEFAULT_MU + k * (career_mu - DEFAULT_MU)
    sigma = max(s * DEFAULT_SIGMA, career_sigma)
(RatingBook.seed). For a grid of (k, s), this puts pseudo season boundaries
into the rated history, builds the career book from the games before each,
seeds, and then scores the next WINDOW games prequentially -- predict each
game from the ratings so far, then rate it.

Scores:
  - LL     raw log loss (lower is better; a coin flip is 0.693)
  - calLL  log loss after fitting one temperature on logit(p). The default
           model is badly overconfident, so raw log loss rewards any setting
           that is merely less sure of itself -- a high sigma would "win" for
           that reason alone. calLL compares the settings' information.
  - acc    share of games the favoured team won
and the same over the first EARLY games after each boundary, where a reset
matters most.

k=0, s=1 is a hard reset; k=1, s=career (sigma column "car") is no reset.
The 2026-10 run on the live DB (1,024 rated games) chose
SOFT_RESET_CARRYOVER = 0.7 / SOFT_RESET_SIGMA = DEFAULT_SIGMA: a hard reset
scored worst (calLL 0.6871 vs 0.6819), and resetting sigma all the way costs
only ~0.002 over keeping more of it.

Run from main/:  uv run python ../scripts/soft_reset_backtest.py
(MONOBOT_DB picks the database, e.g. resources/monobot-live.db.)
"""

import math
import os
import sys

# Make the main/ package layout importable when run from repo root or main/.
MAIN_DIR = os.path.join(os.path.dirname(__file__), "..", "main")
sys.path.insert(0, os.path.abspath(MAIN_DIR))

from services.rating import (  # noqa: E402
    DEFAULT_MU,
    DEFAULT_SIGMA,
    RatingBook,
    predict_win_probability,
)
from services.storage import MatchStore  # noqa: E402

WINDOW = 150  # rated games scored after each boundary
EARLY = 50
# Pseudo boundaries, as indices into the rated games; the real Season 2 start
# is added. Early indices are skipped: there's too little career to seed from.
BOUNDARIES = [400, 475, 550, 650, 725, 800, 874]
SEASON_2_START = "2026-08-21T15:27:02"
KS = [0.0, 0.25, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
SIGMAS = [None, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]  # x DEFAULT_SIGMA; None = keep career sigma
EPS = 1e-6


def score(rated, careers, merge_map, k, s):
    """(p_team1_wins, team1_won, games_since_boundary) for every scored game."""
    out = []
    for b, career in careers.items():
        book = RatingBook(merge_map)
        book.seed(career, k, 0.0 if s is None else s * DEFAULT_SIGMA)
        for i, match in enumerate(rated[b : b + WINDOW]):
            teams = sorted({p.team for p in match.players})
            sides = []
            for t in teams:
                side = []
                for p in match.team(t):
                    r = book.standing_for(p.toon_handle)
                    side.append((r.mu, r.sigma) if r else (DEFAULT_MU, DEFAULT_SIGMA))
                sides.append(side)
            out.append((predict_win_probability(sides[0], sides[1]), match.winning_team == teams[0], i))
            book.rate_match(match)
    return out


def log_loss(rows, temperature=1.0):
    total = 0.0
    for p, won, _ in rows:
        p = min(max(p, EPS), 1 - EPS)
        q = 1 / (1 + math.exp(-temperature * math.log(p / (1 - p))))
        q = min(max(q, EPS), 1 - EPS)
        total -= math.log(q if won else 1 - q)
    return total / len(rows)


def calibrated_log_loss(rows):
    return min(log_loss(rows, t / 100) for t in range(5, 201, 5))


def accuracy(rows):
    return sum((p > 0.5) == won for p, won, _ in rows if p != 0.5) / len(rows)


def main() -> None:
    store = MatchStore(os.environ["MONOBOT_DB"]) if os.environ.get("MONOBOT_DB") else MatchStore()
    merge_map = store.merge_map()
    probe = RatingBook(merge_map)
    rated = [m for _, m in store.all_matches() if probe.is_rateable(m)]
    season2 = next((i for i, m in enumerate(rated) if m.played_at.isoformat() >= SEASON_2_START), None)
    bounds = sorted({b for b in BOUNDARIES + [season2] if b is not None and b < len(rated)})
    print(f"{len(rated)} rated games; boundaries at rated index {bounds}")
    careers = {b: RatingBook.from_matches(rated[:b], merge_map) for b in bounds}

    print(
        f"\n{'k':>5} {'sigma':>6} | {'LL':>6} {'calLL':>6} {'acc':>5} | first {EARLY}: {'LL':>6} {'calLL':>6} {'acc':>5}"
    )
    for k in KS:
        for s in SIGMAS:
            rows = score(rated, careers, merge_map, k, s)
            early = [r for r in rows if r[2] < EARLY]
            label = "car" if s is None else f"{s:.1f}"
            print(
                f"{k:5.2f} {label:>6} | {log_loss(rows):6.4f} {calibrated_log_loss(rows):6.4f} {accuracy(rows):5.3f} | "
                f"{'':9}{log_loss(early):6.4f} {calibrated_log_loss(early):6.4f} {accuracy(early):5.3f}"
            )


if __name__ == "__main__":
    main()
