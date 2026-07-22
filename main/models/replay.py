import datetime

from pydantic import BaseModel


class MatchPlayer(BaseModel):
    name: str  # display name; NOT unique across SC2 accounts
    toon_handle: str  # SC2's unique account id, e.g. "1-S2-1-539205"
    team: int
    race: str  # race actually played in-game (from worker births)
    pick: str | None  # detected monobattle unit pick, None if undetectable
    repick_used: bool | None = None  # blind random only: player repicked their unit
    repick_from: str | None = None  # the unit they repicked away from, if known
    resources_killed: int | None = None  # enemy value destroyed (final stats snapshot)
    econ_killed: int | None = None  # enemy economy value destroyed
    tech_killed: int | None = None  # enemy tech/building value destroyed
    resources_lost: int | None = None  # own value lost
    resources_floated: int | None = None  # median unspent bank over the game
    drop_commands: int | None = None  # transport/Nydus unload commands issued
    static_defense: int | None = None  # defensive structures completed
    bases_before_unit: int | None = None  # town halls completed before first unit
    orbitals: int | None = None  # Orbital Commands owned (Terran)
    lost_all_bases: bool | None = None  # was ever wiped down to zero town halls
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
    comeback_deficit: int | None = None  # winner's worst kill-value deficit
    lead_changes: int | None = None  # meaningful kill-lead flips over the game

    def team(self, number: int) -> list[MatchPlayer]:
        return [p for p in self.players if p.team == number]

    def mvp(self) -> MatchPlayer | None:
        """The player who destroyed the most enemy value, either team — a
        dominant losing performance still earns it (community choice; the
        lobby's top killer is on the losing team in ~24% of games). None when
        there are no kill stats (pre-archive parses)."""
        scored = [p for p in self.players if p.resources_killed]
        if not scored:
            return None
        return max(scored, key=lambda p: p.resources_killed)
