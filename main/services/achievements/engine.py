"""The achievement detector (`AchievementBook`, one chronological pass) and
the glue to the reconciled unlock ledger."""

import datetime
import logging
from collections import Counter

from models.replay import MonobattleMatch
from services import map_versions
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
    """pick -> (win rate, games) over countable matches."""
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


def _match_context(match: MonobattleMatch, map_name: str | None = None) -> _MatchContext:
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
    return _MatchContext(match, match.mvp(), min_kills, team_kills, team_dup, map_name=map_name)


class AchievementBook:
    """Every player's derived state, by canonical handle (use `for_handle`).
    Matches before `epoch` feed only career tallies; None counts all as live."""

    def __init__(
        self,
        merge_map: dict[str, str] | None = None,
        epoch: datetime.datetime | None = None,
        map_names: dict[str, str] | None = None,
    ):
        self._merge = merge_map or {}
        self._epoch = epoch
        # map_hash -> terrain name. Must be the same table every rebuild, or
        # reconcile would revoke Cartographers.
        self._map_names = map_names or {}
        self.histories: dict[str, PlayerHistory] = {}
        self.earned: dict[str, dict[str, Earned]] = {}  # handle -> key -> Earned
        self._last_countable_at: datetime.datetime | None = None
        # pick -> [wins, games] so far in the walk — never all of history, or
        # today's games would change July's underdog (see CLAUDE.md).
        self._unit_counts: dict[str, list[int]] = {}

    @classmethod
    def from_matches(
        cls,
        matches,
        merge_map: dict[str, str] | None = None,
        epoch: datetime.datetime | None = None,
        map_names: dict[str, str] | None = None,
    ) -> "AchievementBook":
        book = cls(merge_map, epoch, map_names)
        for match in sorted(matches, key=lambda m: _naive(m.played_at)):
            if is_countable(match):
                book._tally_match(match)
        return book

    def canonical(self, handle: str) -> str:
        return self._merge.get(handle, handle)

    def _advance_unit_record(self, match: MonobattleMatch) -> None:
        """Fold one match into the running unit record. Called AFTER the match
        is scored, so a game never helps decide its own underdog."""
        for p in match.players:
            if not p.pick:
                continue
            c = self._unit_counts.setdefault(p.pick, [0, 0])
            c[1] += 1
            if p.team == match.winning_team:
                c[0] += 1

    def _rankable_winrates(self) -> dict[str, float]:
        """Win rate of each unit with enough games so far."""
        return {pick: w / g for pick, (w, g) in self._unit_counts.items() if g >= MIN_UNIT_GAMES_FOR_RANKING}

    def _lobby_worst_picks(self, match: MonobattleMatch) -> set[str]:
        """The lobby's lowest-win-rate rankable pick(s), before this match."""
        rates = self._rankable_winrates()
        rated = {p.pick for p in match.players if p.pick in rates}
        if not rated:
            return set()
        worst = min(rates[pick] for pick in rated)
        return {pick for pick in rated if rates[pick] == worst}

    def _tally_match(self, match: MonobattleMatch) -> None:
        ctx = _match_context(match, map_versions.named(match, self._map_names))
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
        self._advance_unit_record(match)

    # -- reads -----------------------------------------------------------

    def for_handle(self, handle: str) -> list[Earned]:
        """Derived earned achievements, rarest first then oldest."""
        return _rarest_first(list(self.earned.get(self.canonical(handle), {}).values()))

    def for_group(self, handles: list[str]) -> list[Earned]:
        """Merged groups collapse to one canonical handle, so any member
        resolves to the same set."""
        return self.for_handle(handles[0]) if handles else []

    def next_up(
        self, handle: str, limit: int = 3, ensure_detail: bool = False
    ) -> list[tuple[AchievementSpec, float, float, list[str]]]:
        """Closest unearned achievements as (spec, current, target, missing),
        most complete first. `ensure_detail` adds a set-based one if none made
        the cut, since those answer "what do I still need?"."""
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
                missing = spec.detail(history) if spec.detail else []
                candidates.append((spec, current, target, missing))
        candidates.sort(key=lambda c: c[1] / c[2], reverse=True)
        top = candidates[:limit]
        if ensure_detail and not any(c[3] for c in top):
            with_detail = next((c for c in candidates if c[3]), None)
            if with_detail is not None:
                top.append(with_detail)
        return top

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
            map_names = self._store.map_version_names() if hasattr(self._store, "map_version_names") else None
            self._book = AchievementBook.from_matches(
                (m for _, m in self._store.all_matches()), merge_map, epoch, map_names
            )
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
    """Silently seed the ledger with everything derivable, once, on a DB with
    history but no unlocks. Returns rows seeded."""
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
    """Silently grant everything derived but unrecorded (after bulk writes).
    Returns rows granted."""
    return len(sweep_new_unlocks(store, cache))


def reconcile(store, cache: AchievementCache) -> tuple[list[tuple[str, Earned]], list[tuple[str, AchievementSpec]]]:
    """Grant and revoke to match the rules; returns (granted, revoked). Safe
    only while grant-only specs are skipped and every spec reads only earlier
    matches — see CLAUDE.md "Achievements" before adding a spec."""
    book = cache.book()
    granted = sweep_new_unlocks(store, cache)
    revoked: list[tuple[str, AchievementSpec]] = []
    rows: list[tuple[str, str]] = []
    for handle, key in store.all_unlocks():
        spec = SPECS_BY_KEY.get(key)
        if spec is None or spec.grant_only:
            continue  # unknown keys are someone else's business; grant-only is not ours
        if key not in book.earned.get(book.canonical(handle), {}):
            rows.append((handle, key))
            revoked.append((book.canonical(handle), spec))
    if rows:
        store.revoke_unlocks(rows)
        logger.info("Reconcile revoked %d unlocks", len(rows))
    return granted, revoked


def sweep_new_unlocks(store, cache: AchievementCache) -> list[tuple[str, Earned]]:
    """sweep_grants returning (canonical handle, Earned) — non-empty only when
    the rules changed, which is what the deploy announcement reports."""
    book = cache.book()
    rows, granted = [], []
    for handle, unlocked in book.earned.items():
        held = {key for key, _ in store.unlocks_for(store.merged_handles(handle))}
        for key, earned in unlocked.items():
            if key not in held:
                rows.append((handle, key, _naive(earned.earned_at).isoformat()))
                granted.append((handle, earned))
    if rows:
        store.record_unlocks(rows)
    return granted


def grant_new_unlocks(store, cache: AchievementCache, match: MonobattleMatch) -> list[tuple[str, Earned]]:
    """Grant `match`'s players their new achievements; returns (name, Earned),
    rarest first. Idempotent."""
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
