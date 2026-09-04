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


# Which killer unit types count as a pick's OWN damage. Most units kill under
# their own name, so only the units whose damage is dealt by something else are
# listed here -- measured over 300 replays (docs/kill-attribution.md), each of
# these accounts for the bulk of that pick's credited value:
#   Carrier 95% Interceptor · SwarmHost 92% Locust · Raven 70% AutoTurret ·
#   BroodLord 70% broodlings · HighTemplar 59% (the rest are Archon merges)
PICK_KILLERS = {
    "Carrier": frozenset({"Carrier", "Interceptor"}),
    "SwarmHost": frozenset({"SwarmHost", "Locust"}),
    "BroodLord": frozenset({"BroodLord", "Broodling", "BroodlingEscort"}),
    "Raven": frozenset({"Raven", "AutoTurret"}),
    "HighTemplar": frozenset({"HighTemplar", "Archon"}),
}

# Kills that are NOT the player's army: their base defending itself, and
# workers pulled to fight. Kept separate so a feature can ask for either.
STATIC_DEFENSE_KILLERS = frozenset(
    {"PhotonCannon", "SpineCrawler", "SpineCrawlerUprooted", "SporeCrawler",
     "SporeCrawlerUprooted", "MissileTurret", "PlanetaryFortress", "Bunker"}
)  # fmt: skip
WORKER_KILLERS = frozenset({"Probe", "SCV", "Drone"})


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
    # Enemy value destroyed, keyed by which of this player's unit types killed
    # it (their pick, but also cannons, workers, and spawned children like
    # Interceptors or Locusts). Empty for games parsed before it was recorded.
    # Its own metric, NOT a breakdown of resources_killed -- see
    # docs/kill-attribution.md.
    kills_by_unit: dict[str, int] = {}

    @property
    def own_kills(self) -> int:
        """Enemy value destroyed by this player's PICK, excluding their static
        defense, their workers, and anything else that got a kill for them.

        Zero for a game parsed before attribution existed, so callers that need
        to tell "no army kills" from "not recorded" should check kills_by_unit
        directly. A few picks deal their damage through spells the tracker
        credits to nobody (Infestor ~34% of its player's value, Sentry ~31%),
        so this UNDERSTATES those specifically -- see docs/kill-attribution.md
        before setting a threshold on one."""
        if not self.pick:
            return 0
        killers = PICK_KILLERS.get(self.pick, frozenset({self.pick}))
        return sum(v for unit, v in self.kills_by_unit.items() if unit in killers)


class MapVersion(BaseModel):
    """The terrain behind one published version of the arcade map. Every
    monobattle is played on the same arcade map ("Monobattle LotV - Map
    Rotation"), so map_name never varies; the rotation happens when the
    author republishes with new terrain, which changes the map's hash. The
    terrain's real name is only in the published map file — see
    services.replay_parser.fetch_map_version."""

    map_hash: str
    name: str  # e.g. "The Ashen Cradle"
    author: str | None = None  # terrain's credited author, when given


class MonobattleMatch(BaseModel):
    file_name: str
    map_name: str  # the arcade map's own name, identical for every game
    map_hash: str = ""  # published-version hash; resolves to the real terrain
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

    @property
    def battle_seconds(self) -> int:
        """How long the fighting actually lasted: the replay's length less the
        pick phase. This is what people mean by how long a game was — the
        draft is dead time on the clock, and it is a whole minute of a
        blind-random game and three to four minutes of a drafted one.

        Clamped at zero for the handful of games that ended before the picks
        were even finished. NOTE that the rating gate, the achievement ledger
        and MatchStore's queries all still measure duration_seconds; moving
        those would change which games rate and who holds which badge."""
        return max(0, self.duration_seconds - self.pick_phase_seconds)

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
