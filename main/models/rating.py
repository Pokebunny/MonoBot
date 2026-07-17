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
    def games(self) -> int:
        return self.wins + self.losses
