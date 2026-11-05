"""Achievements: derived from history, held in the `achievement_unlocks`
ledger, reconciled both ways (see CLAUDE.md for what makes revoking safe).

Career specs read `history.career` (all matches); moment specs read
`history.live` (only games after the deployment's epoch, so feats are never
granted retroactively). Secrets are genuine surprises only, never tiers of a
visible family; career ladders top out past the current record holder.

Thresholds are per-player-game percentiles: 25k kills ≈ p95, 50k ≈ p99, 10k
econ killed ≈ p99, 8k tech ≈ p99, 5x trade ≈ p97, 30k value lost ≈ p98.
"""

from services.achievements.core import (
    CHRONICLER_UPLOADS,
    DROP_PLAY_COMMANDS,
    MIN_UNIT_GAMES_FOR_RANKING,
    RARITIES,
    RARITY_EMOJI,
    UNDERDOG_UNITS,
    AchievementSpec,
    Earned,
    PlayerHistory,
    Tally,
    is_countable,
    is_grant_only,
    is_secret,
)
from services.achievements.engine import (
    AchievementBook,
    AchievementCache,
    ensure_seeded,
    grant_direct,
    grant_new_unlocks,
    ledger_for_group,
    ledger_holder_counts,
    reconcile,
    sweep_grants,
    sweep_new_unlocks,
)
from services.achievements.specs import SECRET_KEYS, SPECS, SPECS_BY_KEY

__all__ = [
    "RARITIES",
    "RARITY_EMOJI",
    "DROP_PLAY_COMMANDS",
    "UNDERDOG_UNITS",
    "MIN_UNIT_GAMES_FOR_RANKING",
    "CHRONICLER_UPLOADS",
    "AchievementSpec",
    "Earned",
    "PlayerHistory",
    "Tally",
    "is_grant_only",
    "is_secret",
    "is_countable",
    "SPECS",
    "SPECS_BY_KEY",
    "SECRET_KEYS",
    "AchievementBook",
    "AchievementCache",
    "ensure_seeded",
    "grant_direct",
    "grant_new_unlocks",
    "sweep_grants",
    "sweep_new_unlocks",
    "ledger_for_group",
    "ledger_holder_counts",
    "reconcile",
]
