from pydantic import BaseModel


class QueuedPlayer(BaseModel):
    discord_id: str
    display_name: str
    sc2_name: str | None  # linked name used for rating; None if unlinked
    mu: float
    sigma: float

    @property
    def rated(self) -> bool:
        """Whether this player has a real rating vs the new-player default."""
        return self.sc2_name is not None


class ProposedMatch(BaseModel):
    team1: list[QueuedPlayer]
    team2: list[QueuedPlayer]
    team1_win_probability: float  # predicted; 0.5 = perfectly balanced

    @property
    def fairness(self) -> float:
        """1.0 = a coin-flip match, 0.0 = fully one-sided."""
        return 1.0 - 2.0 * abs(0.5 - self.team1_win_probability)
