"""Offline comparison of openskill rating models on the community ladder.

Prequential (progressive-validation) evaluation: replay the community matches
(monobot.db) in chronological order and, before rating each game, predict its
outcome from the ratings so far, then update. This is the natural cross-
validation for an online rating system — every game is an out-of-sample test
for the model state that preceded it, with no arbitrary train/test split.

Scores each of openskill's five models on the same match stream:
  - log loss  (proper score; lower is better) — the one that matters most
  - Brier     (proper score; lower is better)
  - accuracy  (share of games the favored team won; higher is better)

Reported over ALL rateable games and over the WARM subset (both teams made
entirely of previously-seen players), where cold-start priors don't drown out
the differences between models.

Run from main/:  uv run python ../scripts/compare_rating_models.py
"""

import math
import os
import sys

# Make the main/ package layout importable when run from repo root or main/.
MAIN_DIR = os.path.join(os.path.dirname(__file__), "..", "main")
sys.path.insert(0, os.path.abspath(MAIN_DIR))

from openskill.models import (  # noqa: E402
    BradleyTerryFull,
    BradleyTerryPart,
    PlackettLuce,
    ThurstoneMostellerFull,
    ThurstoneMostellerPart,
)
from services.rating import MIN_DURATION_SECONDS, MIN_WINNER_CONFIDENCE  # noqa: E402
from services.storage import DEFAULT_DB_PATH, MatchStore  # noqa: E402

MODELS = [
    ("PlackettLuce (current)", PlackettLuce),
    ("BradleyTerryFull", BradleyTerryFull),
    ("BradleyTerryPart", BradleyTerryPart),
    ("ThurstoneMostellerFull", ThurstoneMostellerFull),
    ("ThurstoneMostellerPart", ThurstoneMostellerPart),
]

# Clamp probabilities so a single confident miss can't send log loss to inf.
EPS = 1e-6


def is_rateable(match) -> bool:
    return (
        match.winning_team is not None
        and match.winner_confidence >= MIN_WINNER_CONFIDENCE
        and match.duration_seconds >= MIN_DURATION_SECONDS
        and len({p.team for p in match.players}) == 2
    )


class Scores:
    """Accumulates prequential metrics over a stream of predictions."""

    def __init__(self):
        self.n = 0
        self.log_loss = 0.0
        self.brier = 0.0
        self.correct = 0.0

    def add(self, p: float, outcome: int) -> None:
        p = min(max(p, EPS), 1 - EPS)
        self.n += 1
        self.log_loss += -(outcome * math.log(p) + (1 - outcome) * math.log(1 - p))
        self.brier += (p - outcome) ** 2
        if p == 0.5:
            self.correct += 0.5
        else:
            self.correct += 1.0 if (p > 0.5) == bool(outcome) else 0.0

    def row(self) -> tuple[float, float, float]:
        if self.n == 0:
            return (float("nan"),) * 3
        return (self.log_loss / self.n, self.brier / self.n, self.correct / self.n)


def evaluate(model_factory, matches, merge_map) -> tuple[Scores, Scores]:
    model = model_factory()
    ratings: dict[str, object] = {}  # canonical handle -> openskill rating
    seen: set[str] = set()  # handles that have been in a rated game already
    all_scores, warm_scores = Scores(), Scores()

    def canonical(h: str) -> str:
        return merge_map.get(h, h)

    def get(h: str):
        if h not in ratings:
            ratings[h] = model.rating(name=h)
        return ratings[h]

    for match in matches:
        if not is_rateable(match):
            continue

        team_numbers = sorted({p.team for p in match.players})
        handles = [[canonical(p.toon_handle) for p in match.team(n)] for n in team_numbers]
        teams = [[get(h) for h in group] for group in handles]

        # Predict BEFORE updating: P(team_numbers[0] wins).
        p_first = model.predict_win(teams)[0]
        outcome = 1 if team_numbers[0] == match.winning_team else 0
        all_scores.add(p_first, outcome)
        if all(h in seen for group in handles for h in group):
            warm_scores.add(p_first, outcome)

        # Update.
        ranks = [0 if n == match.winning_team else 1 for n in team_numbers]
        rated = model.rate(teams, ranks=ranks)
        for group, rated_team in zip(handles, rated):
            for h, r in zip(group, rated_team):
                ratings[h] = r
        for group in handles:
            seen.update(group)

    return all_scores, warm_scores


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB_PATH
    store = MatchStore(db_path)
    matches = [m for _, m in store.all_matches()]  # already chronological
    merge_map = store.merge_map()

    rateable = [m for m in matches if is_rateable(m)]
    print(f"DB: {os.path.abspath(db_path)}")
    print(
        f"Matches: {len(matches)} total, {len(rateable)} rateable "
        f"(conf>={MIN_WINNER_CONFIDENCE}, dur>={MIN_DURATION_SECONDS}s, 2 teams)"
    )
    handles = {p.toon_handle for m in rateable for p in m.players}
    print(f"Accounts in rateable games: {len(handles)} ({len(merge_map)} handles merged into groups)\n")

    baseline = "always 0.5"
    print(f"Reference — {baseline}: log loss {math.log(2):.4f}, brier 0.2500, acc 0.500\n")

    header = f"{'model':<26} {'log loss':>9} {'brier':>8} {'acc':>7}    {'log loss':>9} {'brier':>8} {'acc':>7}"
    print(f"{'':<26} {'--- all rateable ---':^26}    {'--- warm subset ---':^26}")
    print(header)
    print("-" * len(header))

    warm_n = 0
    for label, cls in MODELS:
        all_s, warm_s = evaluate(cls, matches, merge_map)
        warm_n = warm_s.n
        a = all_s.row()
        w = warm_s.row()
        print(f"{label:<26} {a[0]:>9.4f} {a[1]:>8.4f} {a[2]:>7.3f}    {w[0]:>9.4f} {w[1]:>8.4f} {w[2]:>7.3f}")

    print(f"\nall rateable n={len(rateable)}, warm n={warm_n} (both teams all previously-seen players)")
    print("Lower log loss / Brier is better; higher accuracy is better. Log loss is the primary discriminator.\n")

    # beta sweep on PlackettLuce: beta is the skill->outcome noise. Default is
    # sigma0/2 ~= 4.17. Bigger beta = flatter (less confident) probabilities,
    # which fixes overconfidence at the cost of accuracy discrimination.
    default_beta = PlackettLuce().beta
    print(f"beta sweep (PlackettLuce; default beta = {default_beta:.3f}):")
    print(f"{'beta (xdefault)':<18} {'log loss':>9} {'brier':>8} {'acc':>7}    {'log loss':>9} {'brier':>8} {'acc':>7}")
    for mult in (1, 2, 3, 4, 6, 8):
        beta = default_beta * mult
        all_s, warm_s = evaluate(lambda b=beta: PlackettLuce(beta=b), matches, merge_map)
        a, w = all_s.row(), warm_s.row()
        print(
            f"{f'{beta:.2f} (x{mult})':<18} {a[0]:>9.4f} {a[1]:>8.4f} {a[2]:>7.3f}    "
            f"{w[0]:>9.4f} {w[1]:>8.4f} {w[2]:>7.3f}"
        )


if __name__ == "__main__":
    main()
