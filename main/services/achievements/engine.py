"""The achievement detector and the unlock ledger glue.

`AchievementBook` DERIVES every player's achievement state from match history in
one chronological pass (a pure function, like ratings). What a player HOLDS is
the append-only `achievement_unlocks` ledger in storage — the book only supplies
candidate state and the diff that decides what to grant and announce. See `core`
for the `Tally`/spec primitives and `specs` for the catalogue."""

import datetime
import logging
from collections import Counter

from models.replay import MonobattleMatch
from services.achievements.core import (
    MIN_UNIT_GAMES_FOR_RANKING,
    RARITIES,
    AchievementSpec,
    Earned,
    PlayerHistory,
    _MatchContext,
    _naive,
    is_countable,
    is_secret,
)
from services.achievements.specs import SPECS, SPECS_BY_KEY

logger = logging.getLogger(__name__)


def _unit_win_rates(matches) -> dict[str, tuple[float, int]]:
    """pick -> (win rate, games) over countable matches. A unit's record, not
    a player's, so account merges don't matter. Pulled live so Overqualified
    never carries a hardcoded win-rate table."""
    counts: dict[str, list[int]] = {}  # pick -> [wins, games]
    for match in matches:
        if not is_countable(match):
            continue
        for p in match.players:
            if not p.pick:
                continue
            c = counts.setdefault(p.pick, [0, 0])
            c[1] += 1
            if p.team == match.winning_team:
                c[0] += 1
    return {pick: (w / g, g) for pick, (w, g) in counts.items() if g}


def _match_context(match: MonobattleMatch) -> _MatchContext:
    scored = [p.resources_killed for p in match.players if p.resources_killed is not None]
    min_kills = min(scored) if len(scored) >= 6 else None
    team_kills: dict[int, int] = {}
    for p in match.players:
        if p.resources_killed is not None:
            team_kills[p.team] = team_kills.get(p.team, 0) + p.resources_killed
    team_dup = {
        n: max(Counter(p.pick for p in match.team(n) if p.pick).values(), default=1)
        for n in {p.team for p in match.players}
    }
    return _MatchContext(match, match.mvp(), min_kills, team_kills, team_dup)


class AchievementBook:
    """Every player's DERIVED achievement state, built in one chronological
    pass. Handles are canonical (post-merge); look up through `for_handle`.
    This is the detector — what players actually hold is the unlock ledger
    (see `ledger_for_group` / `grant_new_unlocks`).

    `epoch` is the deployment's achievement launch time (matches played
    before it feed only the career tally). None means every match counts as
    live — only sensible for tests or throwaway analysis."""

    def __init__(self, merge_map: dict[str, str] | None = None, epoch: datetime.datetime | None = None):
        self._merge = merge_map or {}
        self._epoch = epoch
        self.histories: dict[str, PlayerHistory] = {}
        self.earned: dict[str, dict[str, Earned]] = {}  # handle -> key -> Earned
        self._last_countable_at: datetime.datetime | None = None
        # Dynamic unit win-rate table (see _compute_unit_winrates): rankable
        # units -> win rate, for Overqualified's lobby-worst check.
        self._unit_winrates: dict[str, float] = {}

    @classmethod
    def from_matches(
        cls, matches, merge_map: dict[str, str] | None = None, epoch: datetime.datetime | None = None
    ) -> "AchievementBook":
        book = cls(merge_map, epoch)
        ordered = sorted(matches, key=lambda m: _naive(m.played_at))
        book._compute_unit_winrates(ordered)
        for match in ordered:
            if is_countable(match):
                book._tally_match(match)
        return book

    def canonical(self, handle: str) -> str:
        return self._merge.get(handle, handle)

    def _compute_unit_winrates(self, matches) -> None:
        """Unit win rates, pulled live from match history — no hardcoded rates.
        Only units with enough games are rankable. Recomputed on every rebuild,
        so Overqualified's table tracks the meta on its own."""
        rates = _unit_win_rates(matches)
        self._unit_winrates = {
            pick: wr for pick, (wr, games) in rates.items() if games >= MIN_UNIT_GAMES_FOR_RANKING
        }

    def _lobby_worst_picks(self, match: MonobattleMatch) -> set[str]:
        """The lobby's lowest-win-rate pick(s) among rankable units. Being the
        MVP on one of these means everyone else drew a better unit — a real
        feat regardless of the unit's absolute win rate, so there's no floor."""
        rated = {p.pick for p in match.players if p.pick in self._unit_winrates}
        if not rated:
            return set()
        worst = min(self._unit_winrates[pick] for pick in rated)
        return {pick for pick in rated if self._unit_winrates[pick] == worst}

    def _tally_match(self, match: MonobattleMatch) -> None:
        ctx = _match_context(match)
        ctx.canonical = {p.toon_handle: self.canonical(p.toon_handle) for p in match.players}
        handles = set(ctx.canonical.values())
        ctx.newcomers = {h for h in handles if h not in self.histories}
        ctx.all_veterans = all(h in self.histories and self.histories[h].career.games >= 50 for h in handles)
        played = _naive(match.played_at)
        ctx.community_opening = (
            self._last_countable_at is not None and played - self._last_countable_at >= datetime.timedelta(hours=6)
        )
        self._last_countable_at = played
        ctx.worst_winrate_picks = self._lobby_worst_picks(match)
        live = self._epoch is None or played >= _naive(self._epoch)
        for player in match.players:
            handle = self.canonical(player.toon_handle)
            history = self.histories.setdefault(handle, PlayerHistory())
            history.career.update(player, ctx)
            if live:
                history.live.update(player, ctx)
            unlocked = self.earned.setdefault(handle, {})
            for spec in SPECS:
                if spec.key not in unlocked and spec.check(history):
                    unlocked[spec.key] = Earned(spec, match.played_at)

    # -- reads -----------------------------------------------------------

    def for_handle(self, handle: str) -> list[Earned]:
        """Derived earned achievements, rarest first then oldest."""
        return _rarest_first(list(self.earned.get(self.canonical(handle), {}).values()))

    def for_group(self, handles: list[str]) -> list[Earned]:
        """Merged groups collapse to one canonical handle, so any member
        resolves to the same set."""
        return self.for_handle(handles[0]) if handles else []

    def next_up(self, handle: str, limit: int = 3) -> list[tuple[AchievementSpec, float, float]]:
        """The closest not-yet-earned achievements with measurable progress,
        as (spec, current, target), most complete first."""
        history = self.histories.get(self.canonical(handle))
        if history is None:
            return []
        unlocked = self.earned.get(self.canonical(handle), {})
        candidates = []
        for spec in SPECS:
            if spec.key in unlocked or spec.progress is None or is_secret(spec):
                continue
            current, target = spec.progress(history)
            if current > 0:
                candidates.append((spec, current, target))
        candidates.sort(key=lambda c: c[1] / c[2], reverse=True)
        return candidates[:limit]

    def holder_counts(self) -> dict[str, int]:
        """key -> how many players have earned it (for live rarity display)."""
        counts: dict[str, int] = {}
        for unlocked in self.earned.values():
            for key in unlocked:
                counts[key] = counts.get(key, 0) + 1
        return counts


class AchievementCache:
    """An AchievementBook derived from a match store, rebuilt only when the
    store changes (same pattern as RatingCache)."""

    def __init__(self, store):
        self._store = store
        self._book: AchievementBook | None = None
        self._version = -1

    def book(self) -> AchievementBook:
        if self._book is None or self._version != self._store.change_count:
            merge_map = self._store.merge_map() if hasattr(self._store, "merge_map") else None
            epoch = self._store.achievement_epoch() if hasattr(self._store, "achievement_epoch") else None
            self._book = AchievementBook.from_matches((m for _, m in self._store.all_matches()), merge_map, epoch)
            self._version = self._store.change_count
        return self._book


# -- the unlock ledger (what players actually hold) -----------------------


def _rarest_first(earned: list[Earned]) -> list[Earned]:
    return sorted(earned, key=lambda e: (-RARITIES.index(e.spec.rarity), e.earned_at))


def ledger_for_group(store, handles: list[str]) -> list[Earned]:
    """A merge group's held achievements from the ledger, rarest first.
    Rows whose spec no longer exists are kept in the DB but not shown."""
    out = []
    for key, earned_at in store.unlocks_for(handles):
        spec = SPECS_BY_KEY.get(key)
        if spec is not None:
            out.append(Earned(spec, datetime.datetime.fromisoformat(earned_at)))
    return _rarest_first(out)


def ledger_holder_counts(store, merge_map: dict[str, str] | None = None) -> dict[str, int]:
    """key -> how many players hold it, collapsing merge groups."""
    merge_map = merge_map or {}
    holders: dict[str, set[str]] = {}
    for handle, key in store.all_unlocks():
        holders.setdefault(key, set()).add(merge_map.get(handle, handle))
    return {key: len(hs) for key, hs in holders.items()}


def ensure_seeded(store, cache: AchievementCache) -> int:
    """One-time launch grant: when a deployment first turns achievements on
    over an existing match history, write everything currently derivable into
    the ledger silently (career backfill per design; moment achievements are
    empty because the epoch was just stamped). No-op once any unlock exists,
    and on an empty database — a brand-new community starts announcing from
    its very first game. Returns the number of rows seeded."""
    if store.unlock_count() or not store.match_count():
        return 0
    book = cache.book()
    rows = [
        (handle, earned.spec.key, _naive(earned.earned_at).isoformat())
        for handle, unlocked in book.earned.items()
        for earned in unlocked.values()
    ]
    store.record_unlocks(rows)
    logger.info("Seeded achievement ledger with %d unlocks", len(rows))
    return len(rows)


def grant_direct(store, handle: str, key: str, earned_at: datetime.datetime) -> bool:
    """Grant a ledger-only achievement (one the derived engine can't see,
    e.g. Chronicler's upload count). True if newly granted."""
    held = {k for k, _ in store.unlocks_for(store.merged_handles(handle))}
    if key in held:
        return False
    store.record_unlocks([(handle, key, _naive(earned_at).isoformat())])
    return True


def sweep_grants(store, cache: AchievementCache) -> int:
    """Grant every derived-but-unrecorded achievement across ALL players,
    silently — used after bulk writes (channel backfills, re-parses) where
    per-match announcements would be a wall of stale badges. Returns how many
    rows were granted."""
    book = cache.book()
    rows = []
    for handle, unlocked in book.earned.items():
        held = {key for key, _ in store.unlocks_for(store.merged_handles(handle))}
        rows += [
            (handle, key, _naive(earned.earned_at).isoformat()) for key, earned in unlocked.items() if key not in held
        ]
    if rows:
        store.record_unlocks(rows)
    return len(rows)


def grant_new_unlocks(store, cache: AchievementCache, match: MonobattleMatch) -> list[tuple[str, Earned]]:
    """After a store write touching `match`, grant this match's players any
    derived achievements the ledger doesn't record yet, and return them for
    announcement as (player name, Earned), rarest first. Idempotent: what's
    already in the ledger is never returned again."""
    book = cache.book()
    rows, out, seen = [], [], set()
    for player in match.players:
        handle = book.canonical(player.toon_handle)
        if handle in seen:
            continue
        seen.add(handle)
        held = {key for key, _ in store.unlocks_for(store.merged_handles(player.toon_handle))}
        for key, earned in book.earned.get(handle, {}).items():
            if key not in held:
                rows.append((handle, key, _naive(earned.earned_at).isoformat()))
                out.append((player.name, earned))
    if rows:
        store.record_unlocks(rows)
    out.sort(key=lambda ne: (RARITIES.index(ne[1].spec.rarity), ne[1].earned_at), reverse=True)
    return out
