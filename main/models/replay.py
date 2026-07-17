import datetime

from pydantic import BaseModel


class MatchPlayer(BaseModel):
    name: str
    team: int
    race: str  # race actually played in-game (from worker births)
    pick: str | None  # detected monobattle unit pick, None if undetectable
    repick_used: bool | None = None  # blind random only: player repicked their unit
    unit_counts: dict[str, int]  # normalized army-unit production counts


class MonobattleMatch(BaseModel):
    file_name: str
    map_name: str
    played_at: datetime.datetime  # UTC
    duration_seconds: int
    game_type: str  # e.g. "4v4", from real_type
    pick_mode: str  # "blind_random" | "single_draft" | "tier_draft"
    pick_phase_seconds: int  # when the battle actually started
    players: list[MatchPlayer]
    winning_team: int | None  # None when no winner recorded or inferred
    winner_confidence: float  # 1.0 recorded, <1.0 inferred, 0.0 unknown
    winner_method: str  # "recorded" | "inferred:<signals>" | "unknown"

    def team(self, number: int) -> list[MatchPlayer]:
        return [p for p in self.players if p.team == number]
