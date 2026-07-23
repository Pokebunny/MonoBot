"""Skill ratings for monobattle players.

Uses openskill's Plackett-Luce model (TrueSkill-family Bayesian ratings,
native team support). openskill is isolated behind this module the same way
sc2reader is behind replay_parser.
"""

import logging

from models.rating import PlayerRating
from models.replay import MonobattleMatch
from openskill.models import PlackettLuce

logger = logging.getLogger(__name__)

# Matches below these bars don't move ratings: the in-game counter ignores
# games where someone leaves before ~2 minutes, and uncertain winners go to
# manual confirmation instead of silently rating the wrong team.
MIN_DURATION_SECONDS = 120
MIN_WINNER_CONFIDENCE = 0.7

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
_DEFAULT_DISPLAY = PlayerRating(handle="", name="", mu=DEFAULT_MU, sigma=DEFAULT_SIGMA).display_rating


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
            before[p.toon_handle] = r.display_rating if r is not None else _DEFAULT_DISPLAY
        if not book.rate_match(match):
            return {}
        return {
            p.toon_handle: (before[p.toon_handle], book.rating_for(p.toon_handle).display_rating) for p in match.players
        }
    return {}


class RatingBook:
    """All player ratings, updated match by match (in chronological order)."""

    def __init__(self, merge_map: dict[str, str] | None = None):
        self.ratings: dict[str, PlayerRating] = {}
        # handle -> canonical handle, so one person's linked accounts share a
        # single rating (see MatchStore.merge_map).
        self._merge = merge_map or {}
        self.rated_matches = 0
        self.skipped_matches = 0

    @classmethod
    def from_matches(cls, matches, merge_map: dict[str, str] | None = None) -> "RatingBook":
        """Build a book by replaying matches in chronological order."""
        book = cls(merge_map)
        for match in sorted(matches, key=lambda m: m.played_at):
            book.rate_match(match)
        return book

    def canonical(self, handle: str) -> str:
        return self._merge.get(handle, handle)

    def rating_for(self, handle: str) -> PlayerRating | None:
        """Rating for an account, following any account merge."""
        return self.ratings.get(self.canonical(handle))

    def _get(self, handle: str, name: str) -> PlayerRating:
        """Rating for a (canonical) account. The display name is refreshed to
        the latest one seen (players can rename)."""
        if handle not in self.ratings:
            default = _model.rating(name=handle)
            self.ratings[handle] = PlayerRating(handle=handle, name=name, mu=default.mu, sigma=default.sigma)
        else:
            self.ratings[handle].name = name
        return self.ratings[handle]

    def by_name(self, name: str) -> list[PlayerRating]:
        """All accounts that have played under a display name (case-insensitive),
        most games first. Usually one, but names aren't unique."""
        matches = [r for r in self.ratings.values() if r.name.lower() == name.lower()]
        return sorted(matches, key=lambda r: r.games, reverse=True)

    def is_rateable(self, match: MonobattleMatch) -> bool:
        return (
            match.winning_team is not None
            and match.winner_confidence >= MIN_WINNER_CONFIDENCE
            and match.duration_seconds >= MIN_DURATION_SECONDS
            and len({p.team for p in match.players}) == 2
        )

    def rate_match(self, match: MonobattleMatch) -> bool:
        """Update ratings from one match; returns False if it was skipped."""
        if not self.is_rateable(match):
            self.skipped_matches += 1
            return False

        team_numbers = sorted({p.team for p in match.players})
        teams = [[self._get(self.canonical(p.toon_handle), p.name) for p in match.team(n)] for n in team_numbers]
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

    def leaderboard(self, min_games: int = 1) -> list[PlayerRating]:
        eligible = [r for r in self.ratings.values() if r.games >= min_games]
        return sorted(eligible, key=lambda r: r.ordinal, reverse=True)


class RatingCache:
    """A RatingBook derived from a match store, rebuilt only when the store
    changes. Shared by the cogs that read ratings (leaderboard, matchmaking).

    `store` is duck-typed (needs `.all_matches()` and `.change_count`) so this
    module keeps its one-way dependency on models only."""

    def __init__(self, store):
        self._store = store
        self._book: RatingBook | None = None
        self._version = -1

    def book(self) -> RatingBook:
        if self._book is None or self._version != self._store.change_count:
            merge_map = self._store.merge_map() if hasattr(self._store, "merge_map") else None
            self._book = RatingBook.from_matches((m for _, m in self._store.all_matches()), merge_map)
            self._version = self._store.change_count
        return self._book
