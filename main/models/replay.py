import datetime

from pydantic import BaseModel

# Race of every army unit (the parser drops previews of the wrong race). Not
# the pick pool — see PICKABLE_UNITS.
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

# Never offered as picks: four support units plus two the pool omits
# (confirmed: 42 distinct picks in all history).
_NOT_IN_PICK_POOL = frozenset({"Medivac", "Observer", "WarpPrism", "Overseer", "Mothership", "Viper"})

PICKABLE_UNITS = frozenset(UNIT_RACE) - _NOT_IN_PICK_POOL


# Killers that count as a pick's own damage, for picks that kill through
# something else (share of credited value over 300 replays):
#   Carrier 95% Interceptor · SwarmHost 92% Locust · Raven 70% AutoTurret ·
#   BroodLord 70% broodlings · HighTemplar 59% (the rest are Archon merges)
PICK_KILLERS = {
    "Carrier": frozenset({"Carrier", "Interceptor"}),
    "SwarmHost": frozenset({"SwarmHost", "Locust"}),
    "BroodLord": frozenset({"BroodLord", "Broodling", "BroodlingEscort"}),
    "Raven": frozenset({"Raven", "AutoTurret"}),
    "HighTemplar": frozenset({"HighTemplar", "Archon"}),
}

# Kills that aren't the player's army.
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
    econ_killed: int | None = None
    tech_killed: int | None = None
    resources_lost: int | None = None
    resources_floated: int | None = None  # median unspent bank over the game
    drop_commands: int | None = None
    static_defense: int | None = None
    bases_before_unit: int | None = None  # town halls completed before first unit
    orbitals: int | None = None
    lost_all_bases: bool | None = None  # ever down to zero town halls
    # Early forward cannon / crawler (rules in replay_parser). None = not
    # recorded or unmeasurable.
    cannon_rush: bool | None = None
    spine_rush: bool | None = None
    unit_counts: dict[str, int]  # normalized army-unit production counts
    # Enemy value destroyed by killer unit type. Its own metric, not a breakdown
    # of resources_killed — see docs/kill-attribution.md.
    kills_by_unit: dict[str, int] = {}

    @property
    def own_kills(self) -> int:
        """Value destroyed by the pick itself (through PICK_KILLERS). Understates
        spell casters (Infestor, Sentry) — see docs/kill-attribution.md."""
        if not self.pick:
            return 0
        killers = PICK_KILLERS.get(self.pick, frozenset({self.pick}))
        return sum(v for unit, v in self.kills_by_unit.items() if unit in killers)


class MapVersion(BaseModel):
    """The terrain behind one published version (hash) of the arcade map."""

    map_hash: str
    name: str  # e.g. "The Ashen Cradle"
    author: str | None = None  # terrain's credited author, when given


# At Faster speed. Replay events are in game seconds; duration and the
# in-game timer are real seconds.
GAME_SECONDS_PER_SECOND = 1.4


class MonobattleMatch(BaseModel):
    file_name: str
    map_name: str  # the arcade map's own name, identical for every game
    map_hash: str = ""  # published-version hash; resolves to the real terrain
    played_at: datetime.datetime  # UTC
    # Shared by every recording of one game (unlike played_at, the recorder's clock).
    game_seed: int | None = None
    duration_seconds: int  # real seconds, the clock the in-game timer shows
    game_type: str  # e.g. "4v4", from real_type
    pick_mode: str  # "blind_random" | "single_draft" | "tier_draft"
    pick_phase_seconds: int  # GAME seconds, unlike duration_seconds
    players: list[MatchPlayer]
    winning_team: int | None  # None when no winner recorded or inferred
    winner_confidence: float  # 1.0 recorded, <1.0 inferred, 0.0 unknown
    winner_method: str  # "recorded" | "inferred:<signals>" | "unknown"
    comeback_deficit: int | None = None  # winner's worst deficit in value on the map
    lead_changes: int | None = None  # meaningful flips of the lead in value on the map
    # Someone called for a remake after the battle began (replay_parser._REMAKE_RE).
    remake_called: bool | None = None
    # Vouched for despite storage.rejection_reason (a real game that ended early).
    approved: bool = False

    @property
    def battle_seconds(self) -> int:
        """Real seconds of fighting: duration less the pick phase (converted
        from game seconds). The rating gate still uses duration_seconds."""
        return max(0, self.duration_seconds - round(self.pick_phase_seconds / GAME_SECONDS_PER_SECOND))

    def team(self, number: int) -> list[MatchPlayer]:
        return [p for p in self.players if p.team == number]

    def mvp(self) -> MatchPlayer | None:
        """Top killer on either team (a community choice), or None without stats."""
        scored = [p for p in self.players if p.resources_killed]
        if not scored:
            return None
        return max(scored, key=lambda p: p.resources_killed)
