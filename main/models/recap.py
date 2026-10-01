import datetime

from pydantic import BaseModel


class PlayerRecap(BaseModel):
    """One person's night: their decided games, how the ladder moved them,
    and what they unlocked. Keyed on the canonical (post-merge) handle."""

    handle: str
    name: str  # latest SC2 name seen this session
    wins: int = 0
    losses: int = 0
    mvps: int = 0
    # Season display rating going in and coming out; None when none of their
    # games this session were rated (a 3v3, an unconfirmed winner).
    rating_before: int | None = None
    rating_after: int | None = None
    achievement_keys: list[str] = []  # unlocked this session, rarest first

    @property
    def games(self) -> int:
        return self.wins + self.losses

    @property
    def rating_change(self) -> int | None:
        if self.rating_before is None or self.rating_after is None:
            return None
        return self.rating_after - self.rating_before


class SessionRecap(BaseModel):
    started_at: datetime.datetime  # first game's played_at (UTC)
    ended_at: datetime.datetime  # last game's played_at (UTC)
    games: int  # decided games
    players: list[PlayerRecap]
