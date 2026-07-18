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
