import datetime

from pydantic import BaseModel

# Race of every unit that can appear as a player's army: unit -> race. Used by
# the replay parser, where a preview unit whose race contradicts the race
# actually played is a stale browse. This is NOT the pick pool — see
# PICKABLE_UNITS below.
UNIT_RACE = {
    "Marine": "Terran",
    "Marauder": "Terran",
    "Reaper": "Terran",
    "Ghost": "Terran",
    "Hellion": "Terran",
    "SiegeTank": "Terran",
    "Cyclone": "Terran",
    "WidowMine": "Terran",
    "Thor": "Terran",
    "Viking": "Terran",
    "Medivac": "Terran",
    "Liberator": "Terran",
    "Raven": "Terran",
    "Banshee": "Terran",
    "Battlecruiser": "Terran",
    "Zergling": "Zerg",
    "Baneling": "Zerg",
    "Roach": "Zerg",
    "Ravager": "Zerg",
    "Hydralisk": "Zerg",
    "Lurker": "Zerg",
    "Queen": "Zerg",
    "Mutalisk": "Zerg",
    "Corruptor": "Zerg",
    "BroodLord": "Zerg",
    "SwarmHost": "Zerg",
    "Infestor": "Zerg",
    "Ultralisk": "Zerg",
    "Viper": "Zerg",
    "Overseer": "Zerg",
    "Zealot": "Protoss",
    "Adept": "Protoss",
    "Stalker": "Protoss",
    "Sentry": "Protoss",
    "HighTemplar": "Protoss",
    "DarkTemplar": "Protoss",
    "Archon": "Protoss",
    "Immortal": "Protoss",
    "Colossus": "Protoss",
    "Disruptor": "Protoss",
    "Observer": "Protoss",
    "WarpPrism": "Protoss",
    "Phoenix": "Protoss",
    "VoidRay": "Protoss",
    "Oracle": "Protoss",
    "Tempest": "Protoss",
    "Carrier": "Protoss",
    "Mothership": "Protoss",
}

# Units the map never offers as a pick: the four support units a player gets
# alongside their real army, plus two the pool simply omits. Confirmed by the
# match history — 42 distinct picks across every game on record, never these.
_NOT_IN_PICK_POOL = frozenset({"Medivac", "Observer", "WarpPrism", "Overseer", "Mothership", "Viper"})

# The 42-unit pick pool. Roster-completion achievements (Royal Flush, Winning
# Hand, Exterminator, the zoo set) count and name units from this set, so a
# player is never told to go win with a unit the map cannot deal them.
PICKABLE_UNITS = frozenset(UNIT_RACE) - _NOT_IN_PICK_POOL


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
