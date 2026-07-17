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

_model = PlackettLuce()


class RatingBook:
    """All player ratings, updated match by match (in chronological order)."""

    def __init__(self):
        self.ratings: dict[str, PlayerRating] = {}
        self.rated_matches = 0
        self.skipped_matches = 0

    def _get(self, name: str) -> PlayerRating:
        if name not in self.ratings:
            default = _model.rating(name=name)
            self.ratings[name] = PlayerRating(name=name, mu=default.mu, sigma=default.sigma)
        return self.ratings[name]

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
        teams = [[self._get(p.name) for p in match.team(n)] for n in team_numbers]
        os_teams = [[_model.create_rating([r.mu, r.sigma], name=r.name) for r in team] for team in teams]
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
