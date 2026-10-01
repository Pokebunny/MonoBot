"""Skill ratings for monobattle players.

Uses openskill's Plackett-Luce model (TrueSkill-family Bayesian ratings,
native team support). openskill is isolated behind this module the same way
sc2reader is behind replay_parser.
"""

import itertools
import logging

from models.rating import DuoRecord, PlayerRating, UnitRating
from models.replay import MonobattleMatch
from openskill.models import PlackettLuce

logger = logging.getLogger(__name__)

# Matches below these bars don't move ratings: the in-game counter ignores
# games where someone leaves before ~2 minutes, and uncertain winners go to
# manual confirmation instead of silently rating the wrong team.
MIN_DURATION_SECONDS = 120
MIN_WINNER_CONFIDENCE = 0.7
# Only full 4v4s are rated: a 3v3 or an uneven lobby is a different game, and
# scoring it on the same scale muddies the ladder.
TEAM_SIZE = 4

# Games needed to appear on the leaderboard and hold a rank; players below it
# still have a rating, shown as unranked.
MIN_RANKED_GAMES = 5

_model = PlackettLuce()

# Rating a brand-new/unlinked player starts with, from the model's prior.
_default = _model.rating()
DEFAULT_MU = _default.mu
DEFAULT_SIGMA = _default.sigma


def predict_win_probability(team1: list[tuple[float, float]], team2: list[tuple[float, float]]) -> float:
    """Predicted probability that team1 beats team2, from (mu, sigma) pairs.
    0.5 means an evenly matched game. Keeps openskill calls in this module."""
    t1 = [_model.create_rating([mu, sigma]) for mu, sigma in team1]
    t2 = [_model.create_rating([mu, sigma]) for mu, sigma in team2]
    return _model.predict_win([t1, t2])[0]


# Display rating a player carries before their first rated game (the model's
# prior, run through PlayerRating.display_rating).
DEFAULT_DISPLAY = PlayerRating(handle="", name="", mu=DEFAULT_MU, sigma=DEFAULT_SIGMA).display_rating


def match_rating_deltas(matches, match_id: int, merge_map: dict[str, str] | None = None) -> dict[str, tuple[int, int]]:
    """For the match with id `match_id`, each participant's (before, after)
    display rating, computed at the match's true chronological position by
    replaying history up to and through it. Keyed by the player's own
    toon_handle (so callers can look up by MatchPlayer without a merge map).

    Empty when the match didn't move ratings (unrateable — no winner, low
    confidence, too short), which is exactly when callers should say so rather
    than show a change. `matches` is an iterable of (id, match) pairs, e.g.
    MatchStore.all_matches()."""
    book = RatingBook(merge_map)
    ordered = sorted(matches, key=lambda im: im[1].played_at)
    for mid, match in ordered:
        if mid != match_id:
            book.rate_match(match)
            continue
        before = {}
        for p in match.players:
            r = book.rating_for(p.toon_handle)
            before[p.toon_handle] = r.display_rating if r is not None else DEFAULT_DISPLAY
        if not book.rate_match(match):
            return {}
        return {
            p.toon_handle: (before[p.toon_handle], book.rating_for(p.toon_handle).display_rating) for p in match.players
        }
    return {}


def duo_records(matches, merge_map: dict[str, str] | None = None) -> dict[tuple[str, str], DuoRecord]:
    """Every pair that has played on the same team, keyed by their two
    canonical handles (sorted). `matches` is an iterable of MonobattleMatch,
    e.g. (m for _, m in store.all_matches()).

    Ratings are replayed chronologically so each game is scored against what
    the model believed BEFORE it — the same walk RatingBook.from_matches does,
    with one extra prediction per match to bank the pair's expected wins.
    Unrateable games are skipped, so a pair's record here matches the one the
    ladder counts.

    Each pair also carries a rating of its own; see _rate_duo."""
    book = RatingBook(merge_map)
    records: dict[tuple[str, str], DuoRecord] = {}
    for match in sorted(matches, key=lambda m: m.played_at):
        if book.is_rateable(match):
            _tally_duos(book, match, records)
        book.rate_match(match)
    return records


def _tally_duos(book: "RatingBook", match: MonobattleMatch, records: dict[tuple[str, str], DuoRecord]) -> None:
    """Credit one rateable match to every pair of teammates in it. Called
    before the match is rated, so both the prediction and the individual
    ratings used here are the pre-match ones."""
    team_numbers = sorted({p.team for p in match.players})
    sides = [match.team(n) for n in team_numbers]
    probability = predict_win_probability(*[[_prior(book, p) for p in side] for side in sides])
    # By canonical handle: merged accounts are one person, so a pair is counted
    # once however either of them was logged in.
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
    """Update the pair's own rating from one game.

    The pair is rated as a single entity standing in for its two roster slots:
    its side of the match is [the pair] + their other two teammates, against
    the four opponents, everyone else entering at their individual rating of
    the moment. So the number answers "how strong is this pair" — opponent-
    adjusted the way raw win rate is not, and a level rather than a residual,
    which is why it holds up across halves of history where wins-above-
    expected does not.

    It is mostly, but not only, the two players' individual skill: with the
    combined skill regressed out, what is left still correlates across halves,
    so a little of it is the pair itself."""
    entity = _model.create_rating([record.mu, record.sigma], name=str(record.handles))
    ranks = [0, 1] if won else [1, 0]
    updated = _model.rate([[entity] + teammates, opponents], ranks=ranks)[0][0]
    record.mu, record.sigma = updated.mu, updated.sigma


def _prior(book: "RatingBook", player) -> tuple[float, float]:
    """A player's (mu, sigma) going into a match — the model's prior for
    anyone who hasn't played yet."""
    rating = book.rating_for(player.toon_handle)
    return (rating.mu, rating.sigma) if rating is not None else (DEFAULT_MU, DEFAULT_SIGMA)


# A unit starts far more certain than a new player: there are only ~45 picks,
# each with hundreds of games, and a tight prior shrinks the rarely-picked
# ones toward the average instead of letting a lucky 10 games top the board.
# Of the priors tried against the live history (a half and a quarter of a
# player's), a quarter predicted the later half of it best.
UNIT_PRIOR_SIGMA = DEFAULT_SIGMA / 4


def unit_ratings(matches, merge_map: dict[str, str] | None = None) -> dict[str, UnitRating]:
    """Every unit pick's rating, keyed by pick. `matches` is an iterable of
    MonobattleMatch, e.g. (m for _, m in store.all_matches()).

    Each unit is an extra member of its picker's team, so a side is its four
    players plus their four picks and its strength is the sum of all eight.
    The players enter at their ladder rating from before the game and are
    never changed by this walk — only the units learn. So a unit's rating is
    what the pick adds beyond the skill of whoever picked it, against the
    players and picks it faced: the adjustment raw win rate can't make, where
    a unit good players favour looks strong for their sake.

    Measured on the live history this holds up the way the duo rating does:
    a unit's rating in one half of the games correlates with the other half
    (r ~ +0.6), and adding unit ratings to the ladder's win prediction helps
    out of sample by more than merely padding each side with untrained slots
    does. Letting players and units learn together was tried and predicted
    slightly worse, besides moving the ladder — which this must not do.

    Only games where every pick is known count; a side missing a unit would
    be scored as if that slot were an average pick. Career-wide."""
    book = RatingBook(merge_map)
    units: dict[str, UnitRating] = {}
    for match in sorted(matches, key=lambda m: m.played_at):
        if book.is_rateable(match) and all(p.pick for p in match.players):
            _rate_units(book, match, units)
        book.rate_match(match)
    return units


def _rate_units(book: "RatingBook", match: MonobattleMatch, units: dict[str, UnitRating]) -> None:
    """Update every pick in one match. Called before the match is rated, so
    the players enter at their pre-match ratings."""
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
    # Every slot is scored against the same pre-match unit rating, then the
    # changes are pooled: a pick fielded twice in one game (or on both sides)
    # gets both slots' mu moves, rather than the last slot overwriting the
    # first. Sigma takes the tightest slot's: each update also adds the
    # model's per-game drift (tau), which must count once per game, not once
    # per slot.
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
    """The average pick's mu, weighted by how often each is fielded — the zero
    a unit's bonus is shown against. Not the prior: the picks' ratings only
    mean anything relative to each other, since every side fields four."""
    units = list(units)
    games = sum(u.games for u in units)
    return sum(u.mu * u.games for u in units) / games if games else DEFAULT_MU


class RatingBook:
    """All player ratings, updated match by match (in chronological order)."""

    def __init__(self, merge_map: dict[str, str] | None = None, by_race: bool = False):
        self.ratings: dict[str, PlayerRating] = {}
        # handle -> canonical handle, so one person's linked accounts share a
        # single rating (see MatchStore.merge_map).
        self._merge = merge_map or {}
        # by_race: each person holds a separate rating per race, and every game
        # scores the race they played against the races their opponents
        # played. One walk rates all three, so a race board's opponents enter
        # at their rating on the race they were on, not their overall one.
        # Entries are keyed "handle/race"; PlayerRating.handle stays the
        # canonical handle so names and links resolve as on the main board.
        self._by_race = by_race
        self.rated_matches = 0
        self.skipped_matches = 0

    @classmethod
    def from_matches(cls, matches, merge_map: dict[str, str] | None = None, by_race: bool = False) -> "RatingBook":
        """Build a book by replaying matches in chronological order."""
        book = cls(merge_map, by_race)
        for match in sorted(matches, key=lambda m: m.played_at):
            book.rate_match(match)
        return book

    def canonical(self, handle: str) -> str:
        return self._merge.get(handle, handle)

    def rating_for(self, handle: str) -> PlayerRating | None:
        """Rating for an account, following any account merge."""
        return self.ratings.get(self.canonical(handle))

    def _get(self, player) -> PlayerRating:
        """Rating for a match participant's (canonical) account — on the race
        they played, in a by-race book. The display name is refreshed to the
        latest one seen (players can rename)."""
        handle = self.canonical(player.toon_handle)
        race = player.race if self._by_race else None
        key = f"{handle}/{race}" if race else handle
        if key not in self.ratings:
            default = _model.rating(name=key)
            self.ratings[key] = PlayerRating(
                handle=handle, name=player.name, race=race, mu=default.mu, sigma=default.sigma
            )
        else:
            self.ratings[key].name = player.name
        return self.ratings[key]

    def is_rateable(self, match: MonobattleMatch) -> bool:
        return (
            match.winning_team is not None
            and match.winner_confidence >= MIN_WINNER_CONFIDENCE
            and match.duration_seconds >= MIN_DURATION_SECONDS
            and len({p.team for p in match.players}) == 2
            and all(len(match.team(t)) == TEAM_SIZE for t in {p.team for p in match.players})
        )

    def rate_match(self, match: MonobattleMatch) -> bool:
        """Update ratings from one match; returns False if it was skipped."""
        if not self.is_rateable(match):
            self.skipped_matches += 1
            return False

        team_numbers = sorted({p.team for p in match.players})
        teams = [[self._get(p) for p in match.team(n)] for n in team_numbers]
        os_teams = [[_model.create_rating([r.mu, r.sigma], name=r.handle) for r in team] for team in teams]
        # ranks: lower is better; winner gets 0.
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


class RatingCache:
    """A RatingBook derived from a match store, rebuilt only when the store
    changes. Shared by the cogs that read ratings (leaderboard, matchmaking).

    Ratings are season-scoped: the book replays only matches inside the open
    season's window, so starting a season is a hard reset (everyone back to
    the prior) without deleting anything. `career=True` ignores season bounds.
    Match history, profile stats and achievements are NOT season-scoped and
    read the store directly.

    `store` is duck-typed (needs `.all_matches()` and `.change_count`) so this
    module keeps its one-way dependency on models only."""

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
        # Season turnover changes the window without necessarily changing the
        # matches, so it invalidates the book independently of change_count.
        if self._book is None or self._version != self._store.change_count or self._season_id != season_id:
            merge_map = self._store.merge_map() if hasattr(self._store, "merge_map") else None
            matches = self._store.all_matches() if season is None else self._store.season_matches(season)
            self._book = RatingBook.from_matches((m for _, m in matches), merge_map)
            self._version = self._store.change_count
            self._season_id = season_id
        return self._book


class DuoCache:
    """duo_records for a store, rebuilt only when the store changes.

    Career-wide and not season-scoped, for the same reason MVP rate isn't: a
    pair needs a lot of games together before their record says anything, and
    one season never holds enough of them. Cached because the walk rates every
    pair in every game — twelve model updates per match, an order more work
    than the ladder's own book — and both !duos and !h2h read it."""

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


class UnitCache:
    """unit_ratings for a store, rebuilt only when the store changes. Career-
    wide like the duo board: most picks get a few dozen games a season, too
    few to rate them on."""

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
