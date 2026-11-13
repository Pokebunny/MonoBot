"""Skill ratings (openskill Plackett-Luce, isolated behind this module)."""

import itertools
import logging

from models.rating import DuoRecord, PlayerRating, UnitRating
from models.replay import MonobattleMatch
from openskill.models import PlackettLuce

logger = logging.getLogger(__name__)

# Below these bars a match doesn't move ratings: the in-game counter ignores
# sub-2-minute games, and uncertain winners go to manual confirmation.
MIN_DURATION_SECONDS = 120
MIN_WINNER_CONFIDENCE = 0.7
TEAM_SIZE = 4  # only full 4v4s are rated

MIN_RANKED_GAMES = 5  # below this a player is shown unranked

_model = PlackettLuce()

_default = _model.rating()
DEFAULT_MU = _default.mu
DEFAULT_SIGMA = _default.sigma


def long_enough(match: MonobattleMatch) -> bool:
    """Past MIN_DURATION_SECONDS or approved on upload. Every duration gate
    goes through this or its SQL twins in MatchStore."""
    return match.duration_seconds >= MIN_DURATION_SECONDS or match.approved


def predict_win_probability(team1: list[tuple[float, float]], team2: list[tuple[float, float]]) -> float:
    """P(team1 beats team2), from (mu, sigma) pairs."""
    t1 = [_model.create_rating([mu, sigma]) for mu, sigma in team1]
    t2 = [_model.create_rating([mu, sigma]) for mu, sigma in team2]
    return _model.predict_win([t1, t2])[0]


_FRESH = PlayerRating(handle="", name="", mu=DEFAULT_MU, sigma=DEFAULT_SIGMA)
DEFAULT_DISPLAY = _FRESH.display_rating
DEFAULT_RAW = _FRESH.raw_rating

# Soft reset: mu keeps SOFT_RESET_CARRYOVER of its distance from the prior;
# sigma rises to SOFT_RESET_SIGMA, never falls. Chosen by
# scripts/soft_reset_backtest.py (a hard reset predicted worst; resetting sigma
# fully costs almost nothing and keeps the season a fresh climb).
SOFT_RESET_CARRYOVER = 0.7
SOFT_RESET_SIGMA = DEFAULT_SIGMA

# First-game mu by reported level (beginner < Diamond, mid Diamond-Master, top
# GM); sigma stays at the prior so a wrong pick washes out. The prior is the
# established players' median (~Diamond-Master); beginner/top sit near their
# 15th/90th percentiles.
SKILL_LEVEL_MU = {"beginner": 21.0, "mid": DEFAULT_MU, "top": 30.0}


def level_mu(level: str | None) -> float:
    """The starting mu for a reported level; the prior when there's none."""
    return SKILL_LEVEL_MU.get(level, DEFAULT_MU)


def match_rating_deltas(
    matches, match_id: int, merge_map: dict[str, str] | None = None, start: "RatingBook | None" = None
) -> dict[str, tuple[int, int]]:
    """Each participant's (before, after) display rating for `match_id`, at its
    chronological position, keyed by their own toon_handle. Empty when the match
    was unrateable. `matches` is (id, match) pairs; `start` (e.g. a
    season_start book) is consumed."""
    book = start if start is not None else RatingBook(merge_map)
    ordered = sorted(matches, key=lambda im: im[1].played_at)
    for mid, match in ordered:
        if mid != match_id:
            book.rate_match(match)
            continue
        before = {}
        for p in match.players:
            r = book.standing_for(p.toon_handle)
            before[p.toon_handle] = r.display_rating if r is not None else DEFAULT_DISPLAY
        if not book.rate_match(match):
            return {}
        return {
            p.toon_handle: (before[p.toon_handle], book.rating_for(p.toon_handle).display_rating) for p in match.players
        }
    return {}


def duo_records(matches, merge_map: dict[str, str] | None = None) -> dict[tuple[str, str], DuoRecord]:
    """Every pair of teammates, keyed by sorted canonical handles. Each game is
    scored against pre-match ratings; unrateable games are skipped, as on the
    ladder. See CLAUDE.md "Pair stats" for what the numbers mean."""
    book = RatingBook(merge_map)
    records: dict[tuple[str, str], DuoRecord] = {}
    for match in sorted(matches, key=lambda m: m.played_at):
        if book.is_rateable(match):
            _tally_duos(book, match, records)
        book.rate_match(match)
    return records


def _tally_duos(book: "RatingBook", match: MonobattleMatch, records: dict[tuple[str, str], DuoRecord]) -> None:
    """Credit one rateable match to every teammate pair. Call before rating it."""
    team_numbers = sorted({p.team for p in match.players})
    sides = [match.team(n) for n in team_numbers]
    probability = predict_win_probability(*[[_prior(book, p) for p in side] for side in sides])
    rosters = [{book.canonical(p.toon_handle): p.name for p in side} for side in sides]
    solo = {
        book.canonical(p.toon_handle): _model.create_rating(list(_prior(book, p)), name=book.canonical(p.toon_handle))
        for p in match.players
    }
    for index, (number, roster, chance) in enumerate(zip(team_numbers, rosters, (probability, 1 - probability))):
        won = number == match.winning_team
        opponents = [solo[h] for h in rosters[1 - index]]
        for pair in itertools.combinations(sorted(roster), 2):
            record = records.get(pair)
            if record is None:
                record = records[pair] = DuoRecord(handles=pair, names=pair, mu=DEFAULT_MU, sigma=DEFAULT_SIGMA)
            record.names = (roster[pair[0]], roster[pair[1]])  # latest names win
            record.expected_wins += chance
            if won:
                record.wins += 1
            else:
                record.losses += 1
            _rate_duo(record, [solo[h] for h in roster if h not in pair], opponents, won)


def _rate_duo(record: DuoRecord, teammates: list, opponents: list, won: bool) -> None:
    """Rate the pair as one entity filling its two slots, beside its other two
    teammates and against the four opponents at their individual ratings."""
    entity = _model.create_rating([record.mu, record.sigma], name=str(record.handles))
    ranks = [0, 1] if won else [1, 0]
    updated = _model.rate([[entity] + teammates, opponents], ranks=ranks)[0][0]
    record.mu, record.sigma = updated.mu, updated.sigma


def _prior(book: "RatingBook", player) -> tuple[float, float]:
    """A player's pre-match (mu, sigma), or the prior."""
    rating = book.rating_for(player.toon_handle)
    return (rating.mu, rating.sigma) if rating is not None else (DEFAULT_MU, DEFAULT_SIGMA)


# Tight, so rarely-picked units shrink toward the average. A quarter of a
# player's sigma out-predicted a half on the live history.
UNIT_PRIOR_SIGMA = DEFAULT_SIGMA / 4


def unit_ratings(matches, merge_map: dict[str, str] | None = None, season=None) -> dict[str, UnitRating]:
    """Every pick's rating. Each unit is an extra member of its picker's team;
    players enter at their pre-match ladder rating and only units learn (see
    CLAUDE.md "Unit ratings"). Games with an unknown pick are skipped.

    With `season`, only its games move the units, but players still walk all
    of `matches` at their career rating — a hard reset would put everyone at
    the prior and the units would soak up who picked them."""
    book = RatingBook(merge_map)
    units: dict[str, UnitRating] = {}
    for match in sorted(matches, key=lambda m: m.played_at):
        counted = season is None or season.contains(match.played_at.isoformat())
        if counted and book.is_rateable(match) and all(p.pick for p in match.players):
            _rate_units(book, match, units)
        book.rate_match(match)
    return units


def _rate_units(book: "RatingBook", match: MonobattleMatch, units: dict[str, UnitRating]) -> None:
    """Update every pick in one match. Call before rating it."""
    team_numbers = sorted({p.team for p in match.players})
    sides = [match.team(n) for n in team_numbers]
    for p in match.players:
        if p.pick not in units:
            units[p.pick] = UnitRating(pick=p.pick, mu=DEFAULT_MU, sigma=UNIT_PRIOR_SIGMA)
    teams = [
        [_model.create_rating(list(_prior(book, p))) for p in side]
        + [_model.create_rating([units[p.pick].mu, units[p.pick].sigma]) for p in side]
        for side in sides
    ]
    ranks = [0 if n == match.winning_team else 1 for n in team_numbers]
    rated = _model.rate(teams, ranks=ranks)
    # Pool the slots: a pick fielded twice sums both mu moves, and takes the
    # tightest sigma so the per-game drift (tau) counts once, not per slot.
    moves: dict[str, list[float]] = {}  # pick -> [summed mu change, new sigma]
    for side, new, rank in zip(sides, rated, ranks):
        for p, after in zip(side, new[len(side) :]):
            unit = units[p.pick]
            move = moves.setdefault(p.pick, [0.0, after.sigma])
            move[0] += after.mu - unit.mu
            move[1] = min(move[1], after.sigma)
            if rank == 0:
                unit.wins += 1
            else:
                unit.losses += 1
    for pick, (mu_change, sigma) in moves.items():
        units[pick].mu += mu_change
        units[pick].sigma = sigma


def unit_baseline(units) -> float:
    """Games-weighted average pick mu: the zero unit bonuses are shown against.
    Not the prior — unit ratings are only meaningful relative to each other."""
    units = list(units)
    games = sum(u.games for u in units)
    return sum(u.mu * u.games for u in units) / games if games else DEFAULT_MU


class RatingBook:
    """All player ratings, updated match by match (in chronological order)."""

    def __init__(self, merge_map: dict[str, str] | None = None, by_race: bool = False):
        self.ratings: dict[str, PlayerRating] = {}  # only players who have played
        self._merge = merge_map or {}  # handle -> canonical handle
        # by_race: one rating per person per race, keyed "handle/race"; opponents
        # enter at their rating on the race they played.
        self._by_race = by_race
        self.seeds: dict[str, PlayerRating] = {}  # soft-reset starting points
        self.priors: dict[str, float] = {}  # canonical handle -> reported-level mu
        self.rated_matches = 0
        self.skipped_matches = 0

    @classmethod
    def from_matches(cls, matches, merge_map: dict[str, str] | None = None, by_race: bool = False) -> "RatingBook":
        return cls(merge_map, by_race).replay(matches)

    def replay(self, matches) -> "RatingBook":
        for match in sorted(matches, key=lambda m: m.played_at):
            self.rate_match(match)
        return self

    def seed(self, career: "RatingBook", carryover: float, sigma: float) -> None:
        """Soft reset from `career` (built in the same by_race mode)."""
        for key, r in career.ratings.items():
            self.seeds[key] = PlayerRating(
                handle=r.handle,
                name=r.name,
                race=r.race,
                mu=DEFAULT_MU + carryover * (r.mu - DEFAULT_MU),
                sigma=max(sigma, r.sigma),
            )

    def canonical(self, handle: str) -> str:
        return self._merge.get(handle, handle)

    def rating_for(self, handle: str) -> PlayerRating | None:
        """None until they've played in this book, even with a seed."""
        return self.ratings.get(self.canonical(handle))

    def standing_for(self, handle: str) -> PlayerRating | None:
        """Rating, else seed, else reported level, else None (the prior). For
        matchmaking and deltas; boards use rating_for. Overall books only."""
        key = self.canonical(handle)
        standing = self.ratings.get(key) or self.seeds.get(key)
        if standing is None and key in self.priors:
            standing = PlayerRating(handle=key, name="", mu=self.priors[key], sigma=DEFAULT_SIGMA)
        return standing

    def _get(self, player) -> PlayerRating:
        """A participant's entry, created on first sight; refreshes the name."""
        handle = self.canonical(player.toon_handle)
        race = player.race if self._by_race else None
        key = f"{handle}/{race}" if race else handle
        if key not in self.ratings:
            seed = self.seeds.get(key)
            mu, sigma = (seed.mu, seed.sigma) if seed else (self.priors.get(handle, DEFAULT_MU), DEFAULT_SIGMA)
            self.ratings[key] = PlayerRating(handle=handle, name=player.name, race=race, mu=mu, sigma=sigma)
        else:
            self.ratings[key].name = player.name
        return self.ratings[key]

    def is_rateable(self, match: MonobattleMatch) -> bool:
        return (
            match.winning_team is not None
            and match.winner_confidence >= MIN_WINNER_CONFIDENCE
            and long_enough(match)
            and len({p.team for p in match.players}) == 2
            and all(len(match.team(t)) == TEAM_SIZE for t in {p.team for p in match.players})
        )

    def rate_match(self, match: MonobattleMatch) -> bool:
        """False if the match was skipped."""
        if not self.is_rateable(match):
            self.skipped_matches += 1
            return False

        team_numbers = sorted({p.team for p in match.players})
        teams = [[self._get(p) for p in match.team(n)] for n in team_numbers]
        os_teams = [[_model.create_rating([r.mu, r.sigma], name=r.handle) for r in team] for team in teams]
        ranks = [0 if n == match.winning_team else 1 for n in team_numbers]

        rated = _model.rate(os_teams, ranks=ranks)
        for team, os_team, rank in zip(teams, rated, ranks):
            for player, os_player in zip(team, os_team):
                player.mu = os_player.mu
                player.sigma = os_player.sigma
                if rank == 0:
                    player.wins += 1
                else:
                    player.losses += 1

        self.rated_matches += 1
        return True

    def leaderboard(self, min_games: int = 1, race: str | None = None) -> list[PlayerRating]:
        """Ranked entries; `race` narrows a by-race book to one race's board."""
        eligible = [r for r in self.ratings.values() if r.games >= min_games and (race is None or r.race == race)]
        return sorted(eligible, key=lambda r: r.ordinal, reverse=True)


def _in_mode(matches, mode: str | None):
    """Matches from (id, match) rows in one pick mode; None keeps all."""
    return (m for _, m in matches if mode is None or m.pick_mode == mode)


def season_start(store, season, by_race: bool = False, mode: str | None = None) -> RatingBook:
    """The book a season's walk starts from: empty (hard reset, or career when
    season is None) or seeded from career ratings at its start (soft reset).
    Reported levels apply in both walks. `mode` filters the seeds' games too."""
    merge_map = store.merge_map()
    priors = level_priors(store, merge_map)
    book = RatingBook(merge_map, by_race)
    book.priors = priors
    if season is not None and season.carryover is not None:
        before = store.all_matches(until=season.started_at)
        career = RatingBook(merge_map, by_race)
        career.priors = priors
        career.replay(_in_mode(before, mode))
        book.seed(career, season.carryover, season.seed_sigma)
    return book


def level_priors(store, merge_map: dict[str, str]) -> dict[str, float]:
    """Reported-level mu by canonical handle."""
    if not hasattr(store, "skill_levels_by_handle"):
        return {}
    return {merge_map.get(h, h): level_mu(level) for h, level in store.skill_levels_by_handle().items()}


# Rush boards, named like picks so they're typed and shown like one.
CANNON_RUSH = "CannonRush"
SPINE_RUSH = "SpineRush"
RUSH_BOARDS = (CANNON_RUSH, SPINE_RUSH)


def boards_of(player) -> list[str]:
    """Their pick plus a board per rush — a rush adds, never replaces."""
    boards = [player.pick] if player.pick else []
    if player.cannon_rush:
        boards.append(CANNON_RUSH)
    if player.spine_rush:
        boards.append(SPINE_RUSH)
    return boards


def pick_ratings(book: RatingBook, matches, picks: set[str] | None = None) -> dict[str, list[PlayerRating]]:
    """Per-unit player boards, best first, keyed by board (boards_of); `picks`
    limits which. `book` is the overall walk to replay on (empty or a
    season_start). Only the focal player's slot is a per-unit entry; the other
    seven enter at their overall standing (see CLAUDE.md)."""
    entries: dict[str, dict[str, PlayerRating]] = {}
    for match in sorted(matches, key=lambda m: m.played_at):
        if book.is_rateable(match):
            _rate_picks(book, match, picks, entries)
        book.rate_match(match)
    return {pick: sorted(board.values(), key=lambda r: r.ordinal, reverse=True) for pick, board in entries.items()}


def _rate_picks(
    book: RatingBook, match: MonobattleMatch, picks: set[str] | None, entries: dict[str, dict[str, PlayerRating]]
) -> None:
    """Score each player on each of their boards. Call before rating the match."""
    team_numbers = sorted({p.team for p in match.players})
    ranks = [0 if n == match.winning_team else 1 for n in team_numbers]
    for focal, board in ((p, b) for p in match.players for b in boards_of(p)):
        if picks is not None and board not in picks:
            continue
        handle = book.canonical(focal.toon_handle)
        entry = entries.setdefault(board, {}).setdefault(
            handle, PlayerRating(handle=handle, name=focal.name, mu=DEFAULT_MU, sigma=DEFAULT_SIGMA)
        )
        entry.name = focal.name
        teams, slot = [], None
        for n in team_numbers:
            side = []
            for p in match.team(n):
                if p is focal:
                    slot = (len(teams), len(side))
                    side.append(_model.create_rating([entry.mu, entry.sigma]))
                else:
                    standing = book.standing_for(p.toon_handle)
                    mu, sigma = (standing.mu, standing.sigma) if standing else (DEFAULT_MU, DEFAULT_SIGMA)
                    side.append(_model.create_rating([mu, sigma]))
            teams.append(side)
        after = _model.rate(teams, ranks=ranks)[slot[0]][slot[1]]
        entry.mu, entry.sigma = after.mu, after.sigma
        if ranks[slot[0]] == 0:
            entry.wins += 1
        else:
            entry.losses += 1


def pick_board(store, season, pick: str, mode: str | None = None) -> list[PlayerRating]:
    """One unit's player board over a season (None = career)."""
    matches = store.all_matches() if season is None else store.season_matches(season)
    return pick_ratings(season_start(store, season, mode=mode), _in_mode(matches, mode), {pick}).get(pick, [])


def unit_kings(
    boards: dict[str, list[PlayerRating]], min_games: int
) -> list[tuple[str, list[tuple[str, PlayerRating]]]]:
    """#1 on each board (with `min_games`), grouped by person, most #1s first:
    [(canonical handle, [(pick, entry)] best first)]."""
    held: dict[str, list[tuple[str, PlayerRating]]] = {}
    for pick, board in boards.items():
        top = next((r for r in board if r.games >= min_games), None)
        if top is not None:
            held.setdefault(top.handle, []).append((pick, top))
    for tops in held.values():
        tops.sort(key=lambda t: t[1].ordinal, reverse=True)
    return sorted(held.items(), key=lambda kv: (len(kv[1]), kv[1][0][1].ordinal), reverse=True)


def season_book(store, season, by_race: bool = False, mode: str | None = None) -> RatingBook:
    """A season's ratings (None = career). The only way to build a season board,
    so soft resets show everywhere alike."""
    matches = store.all_matches() if season is None else store.season_matches(season)
    return season_start(store, season, by_race, mode).replay(_in_mode(matches, mode))


class RatingCache:
    """The current season's RatingBook (or career's), rebuilt when the store
    changes or the season turns over."""

    def __init__(self, store, career: bool = False):
        self._store = store
        self._career = career
        self._book: RatingBook | None = None
        self._version = -1
        self._season_id: int | None = None

    def _season(self):
        if self._career or not hasattr(self._store, "current_season"):
            return None
        return self._store.current_season()

    def book(self) -> RatingBook:
        season = self._season()
        season_id = season.id if season else None
        if self._book is None or self._version != self._store.change_count or self._season_id != season_id:
            self._book = season_book(self._store, season)
            self._version = self._store.change_count
            self._season_id = season_id
        return self._book


class DuoCache:
    """Career-wide duo_records (a season holds too few games per pair), rebuilt
    when the store changes."""

    def __init__(self, store):
        self._store = store
        self._records: dict[tuple[str, str], DuoRecord] | None = None
        self._version = -1

    def records(self) -> dict[tuple[str, str], DuoRecord]:
        if self._records is None or self._version != self._store.change_count:
            merge_map = self._store.merge_map() if hasattr(self._store, "merge_map") else None
            self._records = duo_records((m for _, m in self._store.all_matches()), merge_map)
            self._version = self._store.change_count
        return self._records


class PickCache:
    """Every unit's career player board, rebuilt when the store (or a link)
    changes."""

    def __init__(self, store):
        self._store = store
        self._boards: dict[str, list[PlayerRating]] | None = None
        self._version = -1

    def boards(self) -> dict[str, list[PlayerRating]]:
        if self._boards is None or self._version != self._store.change_count:
            matches = (m for _, m in self._store.all_matches())
            self._boards = pick_ratings(season_start(self._store, None), matches)
            self._version = self._store.change_count
        return self._boards


class UnitCache:
    """Career-wide unit_ratings, rebuilt when the store changes."""

    def __init__(self, store):
        self._store = store
        self._units: dict[str, UnitRating] | None = None
        self._version = -1

    def units(self) -> dict[str, UnitRating]:
        if self._units is None or self._version != self._store.change_count:
            merge_map = self._store.merge_map() if hasattr(self._store, "merge_map") else None
            self._units = unit_ratings((m for _, m in self._store.all_matches()), merge_map)
            self._version = self._store.change_count
        return self._units
