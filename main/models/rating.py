from pydantic import BaseModel


class PlayerRating(BaseModel):
    handle: str  # SC2 unique account id — the identity ratings are keyed on
    name: str  # latest display name seen for this account
    mu: float
    sigma: float
    wins: int = 0
    losses: int = 0

    @property
    def ordinal(self) -> float:
        """Conservative skill estimate (mu - 3*sigma); leaderboard sort key."""
        return self.mu - 3 * self.sigma

    @property
    def display_rating(self) -> int:
        """A friendlier MMR-style number for players (the raw mu-3*sigma is
        opaque). A fresh player lands near 1000; strong regulars reach ~2200+."""
        return round(self.ordinal * 40 + 1000)

    @property
    def provisional(self) -> bool:
        """Still calibrating — few games, so the rating will move a lot. High
        sigma is exactly the model's 'not sure yet' signal."""
        return self.sigma > 6.0

    @property
    def games(self) -> int:
        return self.wins + self.losses


class DuoRecord(BaseModel):
    """How one pair of teammates has done together. `expected_wins` is the sum
    of the model's pre-match win probability over their shared games, so it
    already accounts for both players' skill, their other two teammates and
    the opposition — the gap between it and `wins` is what the pair did that
    their parts don't explain."""

    handles: tuple[str, str]  # canonical handles, sorted; one entry per pair
    names: tuple[str, str]  # latest display name seen for each, same order
    mu: float  # the PAIR's own rating, fit on their games together
    sigma: float
    wins: int = 0
    losses: int = 0
    expected_wins: float = 0.0

    @property
    def games(self) -> int:
        return self.wins + self.losses

    @property
    def ordinal(self) -> float:
        """Conservative estimate of the pair's strength; the board's sort key."""
        return self.mu - 3 * self.sigma

    @property
    def display_rating(self) -> int:
        """The pair's rating on the same scale a player's is shown in. Halved
        because the entity covers two roster slots, so it reads as "this pair
        plays like two players of about this rating" — directly comparable to
        the two numbers on the ladder next to their names."""
        return round(self.ordinal / 2 * 40 + 1000)

    @property
    def win_rate(self) -> float:
        return self.wins / self.games if self.games else 0.0

    @property
    def synergy(self) -> float:
        """Wins above expectation. Positive means the pair beats the sum of
        its parts. It is a shrunk estimate, not a pure one: winning together a
        lot also lifts both players' individual ratings, which raises
        `expected_wins` and pulls this back toward zero."""
        return self.wins - self.expected_wins
