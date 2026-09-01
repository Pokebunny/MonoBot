"""Career and single-game achievements.

Split responsibilities: this package DERIVES achievement state from match
history (a pure function, like ratings), but what a player HOLDS is the
`achievement_unlocks` ledger in storage — written when a crossing is first
observed at ingest. The ledger is what profiles and the gallery show, and the
derived book supplies candidate state, progress bars, and the diff that
decides what to grant and announce.

The ledger is RECONCILED, not append-only: `reconcile` brings it back in line
with the rules in both directions and hands the caller both lists, so a badge
is never taken away silently. Two properties make revoking safe, and both are
load-bearing — a new spec that breaks either turns a correction into a badge
that randomly disappears:
- specs marked `grant_only` are never revoked, because "the engine did not
  derive it" means "the engine cannot see it" (Chronicler counts uploads);
- every context a spec reads comes from matches BEFORE the one being scored,
  so replaying the same history twice gives the same answer.
Rarity is only a label and moves in either direction; it never touches the
ledger, so a re-tuned tier costs nobody a badge.

Two kinds of spec, split by `Tally` view:
- career achievements read `history.career` — accumulated over ALL stored
  matches, so long-time players get credit for their record (granted
  silently by `ensure_seeded` when a deployment first turns achievements on).
- moment achievements read `history.live` — only matches played after the
  deployment's achievement epoch (stamped in the DB's meta table) count, so
  single-game feats and streaks are earned live at the table, never handed
  out retroactively — including by a future backfill of old replays.

Secret achievements are hidden from the gallery and progress display until
unlocked. Only genuine surprises are flagged secret — tier extensions of
visible families never are (the tier below telegraphs them anyway). Career
tier ladders always top out beyond the current record holder, so every
player, including the most veteran, has a next rung.

Thresholds are calibrated against the community DB + a 90-replay archive
sample (per-player-game percentiles): 25k kills ≈ p95, 50k ≈ p99, 10k econ
killed ≈ p99, 8k tech ≈ p99, 5x trade ≈ p97, 30k value lost ≈ p98.

The code is split across three modules — `core` (the `Tally` and spec
primitives), `specs` (the declarative catalogue), and `engine` (the detector
and unlock ledger) — but the public surface is re-exported here, so callers
keep importing straight from `services.achievements`.
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
    # constants / display
    "RARITIES",
    "RARITY_EMOJI",
    "DROP_PLAY_COMMANDS",
    "UNDERDOG_UNITS",
    "MIN_UNIT_GAMES_FOR_RANKING",
    "CHRONICLER_UPLOADS",
    # types
    "AchievementSpec",
    "Earned",
    "PlayerHistory",
    "Tally",
    "is_grant_only",
    "is_secret",
    "is_countable",
    # catalogue
    "SPECS",
    "SPECS_BY_KEY",
    "SECRET_KEYS",
    # engine + ledger
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
