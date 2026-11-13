from pydantic import BaseModel


class PlayerRating(BaseModel):
    handle: str  # SC2 unique account id — the identity ratings are keyed on
    name: str  # latest display name seen for this account
    race: str | None = None  # set only on a by-race book's entries
    mu: float
    sigma: float
    wins: int = 0
    losses: int = 0

    @property
    def ordinal(self) -> float:
        """mu - 3*sigma: the leaderboard sort key."""
        return self.mu - 3 * self.sigma

    @property
    def display_rating(self) -> int:
        """MMR-style: a fresh player is near 1000, strong regulars ~2200+."""
        return round(self.ordinal * 40 + 1000)

    @property
    def raw_rating(self) -> int:
        """mu on display_rating's scale, with no uncertainty taken off — what
        matchmaking balances on. A fresh player is ~2000 here."""
        return round(self.mu * 40 + 1000)

    @property
    def provisional(self) -> bool:
        return self.sigma > 6.0

    @property
    def games(self) -> int:
        return self.wins + self.losses


class DuoRecord(BaseModel):
    """One pair of teammates. `expected_wins` sums the model's pre-match win
    probability over their shared games."""

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
        return self.mu - 3 * self.sigma

    @property
    def display_rating(self) -> int:
        """Halved (the pair fills two slots) to compare with player ratings."""
        return round(self.ordinal / 2 * 40 + 1000)

    @property
    def win_rate(self) -> float:
        return self.wins / self.games if self.games else 0.0

    @property
    def synergy(self) -> float:
        """Wins above expectation (shrunk: shared wins also raise expectations)."""
        return self.wins - self.expected_wins


class UnitRating(BaseModel):
    """A pick's worth beyond its picker, on the player scale: 2 mu above
    another plays like its picker being 2 mu (80 points) better."""

    pick: str
    mu: float
    sigma: float
    wins: int = 0  # per slot
    losses: int = 0

    @property
    def games(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        return self.wins / self.games if self.games else 0.0

    def points(self, baseline: float) -> int:
        """Display points above `baseline` (the average pick's mu)."""
        return round((self.mu - baseline) * 40)
