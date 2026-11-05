"""Parse monobattle replays into MonobattleMatch (the only sc2reader user).

Pick: the last preview unit the map spawns before the pick phase ends, with
production counts as corroboration and fallback.

Winner: recorded only when every loser leaves before the recorder; otherwise
inferred from army value, gg-concessions and departures, scored by agreement.
"""

import logging
import math
import os
import re
import statistics
from collections import Counter, defaultdict

import sc2reader
from models.replay import UNIT_RACE, MapVersion, MatchPlayer, MonobattleMatch

logger = logging.getLogger(__name__)

# Fallback when the battle-start marker (Spray* decals awarded to everyone) is
# missing.
_DEFAULT_PICK_PHASE_SECONDS = 60

# Pick-mode classification: blind random games start at ~63-76s; both draft
# modes spend minutes in the pick phase first.
_DRAFT_START_THRESHOLD = 120
# Single draft picks come one at a time (first previews staggered over the
# whole phase); tier drafts cluster everyone's previews late and close.
_DRAFT_SPREAD_THRESHOLD = 60

_WORKER_COMMANDS = {"TrainProbe", "TrainSCV", "MorphDrone"}

# Static defense structures, by exact name: variants (uprooted crawlers, the
# map's RenegadeMissileTurret) and Raven AutoTurrets deliberately excluded.
_STATIC_DEFENSE = {
    "PhotonCannon",
    "ShieldBattery",
    "SpineCrawler",
    "SporeCrawler",
    "MissileTurret",
    "Bunker",
    "PlanetaryFortress",
}

# Cannon rush: a cannon this early, nearer an enemy start than one's own. First
# cannons land under ~30% or over ~84% of the way, and 90% of forward ones by
# 3:44; later forward cannons defend mid-game bases.
_CANNON_RUSH_WINDOW_SECONDS = 240

# Spine rush: a crawler in the window at least _CRAWLER_RUSH_REACH across (so a
# proxy hatch). Early spines sit under ~40% or past 70%; first forward spines
# land by 6:00 or after 7:00 (mid-game bases). 60%, not half, because hatches
# 30-60% across are expansions; it loses 3 borderline games of 125.
_CRAWLER_RUSH_WINDOW_SECONDS = 360
_CRAWLER_RUSH_REACH = 0.6
_RUSH_CRAWLERS = {"SpineCrawler", "SporeCrawler"}

# Units that never count toward a player's mono pick: workers, map objects,
# spawned sub-units, and other free/incidental units.
_NOISE_PREFIXES = ("Beacon", "Changeling", "Locust", "Broodling")
_NOISE_UNITS = {
    "Drone",
    "Probe",
    "SCV",
    "MULE",
    "Larva",
    "Egg",
    "InvisibleTargetDummy",
    "Interceptor",
    "AutoTurret",
    "Nuke",
    "KD8Charge",
    "RavenRepairDrone",
    "Overlord",
    "OverlordCocoon",
    "TransportOverlordCocoon",
    "BanelingCocoon",
    "RavagerCocoon",
    "BroodLordCocoon",
    "LurkerEgg",
    "LurkerMPEgg",
    "DisruptorPhased",  # projectile spawned per Disruptor shot
    "ShieldBattery",  # building, but not flagged is_building by sc2reader
}

# Available to everyone; the pick only on extra evidence (or nothing else made):
# - Queen: limited to one per hatchery unless picked
# - Raven: can't use abilities (e.g. AutoTurret) unless picked
_SUPPORT_UNITS = {
    "Queen",
    "Overseer",
    "OverlordTransport",
    "Observer",
    "WarpPrism",
    "Sentry",
    "Medivac",
    "Raven",
}

# Mode/morph variants folded into their canonical unit name.
_NORMALIZE = {
    "ThorAP": "Thor",
    "BattleHellion": "Hellion",
    "AdeptPhaseShift": "Adept",
    "SiegeTankSieged": "SiegeTank",
    "VikingAssault": "Viking",
    "VikingFighter": "Viking",
    "LiberatorAG": "Liberator",
    "WidowMineBurrowed": "WidowMine",
    "WarpPrismPhasing": "WarpPrism",
    "ObserverSiegeMode": "Observer",
    "OverseerSiegeMode": "Overseer",
    "LurkerMP": "Lurker",
    "LurkerMPBurrowed": "Lurker",
    "SwarmHostMP": "SwarmHost",
}

# Units that can never be a pick: Overseers/transport overlords morph from
# Overlords, which every zerg player has regardless of pick.
_NEVER_PICKS = {"Overseer", "OverlordTransport"}

# Morph -> precursors. In production fallback a morph overrides only its own
# precursor (a Mutalisk player's Overseers mustn't override Mutalisk).
_MORPH_PRECURSORS = {
    "Baneling": {"Zergling"},
    "Ravager": {"Roach"},
    "Lurker": {"Hydralisk"},
    "BroodLord": {"Corruptor"},
    "Archon": {"HighTemplar", "DarkTemplar"},
}

_HATCHERIES = {"Hatchery", "Lair", "Hive"}

# New town halls only, not morphs. The Terran start spawns as an
# OrbitalCommand, counted separately on birth.
_BASE_STRUCTURES = {"Nexus", "CommandCenter", "Hatchery"}

# Every town-hall form, morphs and lift-offs included.
_TOWN_HALLS = _BASE_STRUCTURES | {
    "CommandCenterFlying",
    "OrbitalCommand",
    "OrbitalCommandFlying",
    "PlanetaryFortress",
    "Lair",
    "Hive",
}

# Worker births are the ground truth for the race a player actually played
# (lobby race data can't be trusted in monobattles).
_WORKER_RACE = {"Drone": "Zerg", "Probe": "Protoss", "SCV": "Terran"}

# Race of each pickable unit; the roster itself lives in models.replay so the
# achievement engine can share it (see UNIT_RACE there).
_UNIT_RACE = UNIT_RACE

# Solo confidences from blind validation on 446 recorded-winner replays
# (2026-07): net-departures 99.8% (fires 444), army 100% (fires 402), gg 95.8%.
# Army and departures never disagreed there; conflicts go to departures.
_ARMY_DOMINANCE_RATIO = 1.5  # team army value ratio considered decisive
_ARMY_MINIMUM = 2000  # leader must have real army or the snapshot is noise
_GG_WINDOW_SECONDS = 90  # gg this close before leaving = concession
# Standalone gg/ggs/ggwp/ggggg — not the "gg" inside "laggin" or "buggy".
_GG_RE = re.compile(r"(?<![a-z])g{2,}\s*(wp)?s?(?![a-z])")
# remake/rehost/redo/restart anywhere, or re/rm/rmk ending a message ("pls re"),
# never "you're" or "theya re". Battle only: in the pick phase "re" is repick.
_REMAKE_RE = re.compile(r"(?<![a-z'])(?:(?:remake|rehost|redo|re ?start)(?![a-z])|(?:re|rm|rmk)\W*$)")
_RECORDER_LEAVE_WINDOW = 10  # a leave this close to the end is the recorder's
_CONF_RECORDED = 1.0
_CONF_AGREEMENT = 0.9
_CONF_SOLO = {"departures": 0.85, "army": 0.8, "gg": 0.8}
_CONF_GG_CONFLICT = 0.7  # gg is an explicit concession — wins any conflict
_CONF_DEPARTURES_CONFLICT = 0.75  # departures beat army (base races/cannon rush)
_CONF_CONFLICT = 0.0


def _canonical(name: str) -> str | None:
    """Normalize a raw unit-type name; None if it's noise."""
    if name.startswith(_NOISE_PREFIXES) or name in _NOISE_UNITS:
        return None
    if name.endswith("Burrowed"):
        name = name.removesuffix("Burrowed")
    return _NORMALIZE.get(name, name)


def _canonical_killer(name: str) -> str:
    """A killer's name: `_canonical`'s mode merging without its noise filter,
    since workers, Interceptors etc. are exactly the killers to tell apart."""
    if name.endswith("Burrowed"):
        name = name.removesuffix("Burrowed")
    return _NORMALIZE.get(name, name)


class _PlayerTally:
    """Per-player production evidence accumulated from replay events."""

    def __init__(self):
        self.previews: list[str] = []  # pick-phase preview units, in order
        self.preview_times: list[int] = []  # seconds, parallel to previews
        self.last_pick_dialog: int | None = None  # last repick/keep button pressed
        # From each player's last stats snapshot (None = no snapshot seen):
        self.resources_killed: int | None = None  # total enemy value destroyed
        self.econ_killed: int | None = None  # enemy economy value destroyed
        self.tech_killed: int | None = None  # enemy tech/building value destroyed
        self.resources_lost: int | None = None  # own value lost
        # The median, not the final bank: leavers donate theirs to teammates.
        self.floated_samples: list[int] = []
        self.units = Counter()  # post-pick-phase army production
        self.hatcheries = 0
        self.auto_turrets = 0
        self.worker_races = Counter()  # race evidence from worker births
        self.drop_commands = 0  # transport/Nydus unload commands issued
        self.static_defense = 0  # defensive structures completed
        self.base_times: list[int] = []  # town-hall completion seconds
        # Every Zerg makes Queens; they count as production only for a Queen pick.
        self.first_unit_second: int | None = None  # first non-Queen production
        self.first_queen_second: int | None = None
        self.orbitals = 0  # Orbital Commands owned (starting + CC morphs)
        # Town halls still standing, by unit id — morphs and lift-offs keep the
        # same id, so a CC becoming an Orbital never looks like a loss.
        self.live_town_halls: set[int] = set()
        self.lost_all_bases = False  # was ever wiped down to zero town halls
        # Enemy value destroyed, split by which of this player's unit types
        # got the kill. See _tally_events for what does and doesn't land here.
        self.kills_by_unit = Counter()
        self.home: tuple[float, float] | None = None  # where their starting town hall spawned
        self.cannon_starts: list[tuple[int, float, float]] = []  # (second, x, y) per cannon begun
        self.crawler_starts: list[tuple[int, float, float]] = []  # same, per spine/spore crawler

    def race(self, fallback: str) -> str:
        if self.worker_races:
            return self.worker_races.most_common(1)[0][0]
        return fallback


def _detect_pick(tally: _PlayerTally, play_race: str) -> str | None:
    """Last pick-phase preview unit wins; fall back to production counts."""
    valid_previews = [u for u in tally.previews if _UNIT_RACE.get(u, play_race) == play_race]
    if valid_previews:
        return valid_previews[-1]
    counts = tally.units
    support = set(_SUPPORT_UNITS)
    # Queens beyond one-per-hatchery mean Queen is the pick, not support.
    if counts["Queen"] > tally.hatcheries:
        support.discard("Queen")
    # Ravens can only use abilities when they're the picked unit.
    if tally.auto_turrets > 0:
        support.discard("Raven")
    real = {u: n for u, n in counts.items() if u not in support and u not in _NEVER_PICKS}
    pool = real or {u: n for u, n in counts.items() if u not in _NEVER_PICKS}
    if not pool:
        return None
    pick = max(pool, key=lambda u: pool[u])
    # Meaningful morph volume overrides the precursor it was made from.
    for morph, precursors in _MORPH_PRECURSORS.items():
        if pick in precursors and pool.get(morph, 0) >= 3:
            pick = morph
    return pick


def _rushed(
    tally: _PlayerTally,
    starts: list[tuple[int, float, float]],
    enemy_homes: list[tuple[float, float]],
    game_start: int,
    window: int,
    reach: float,
) -> bool | None:
    """Any of `starts` (second, x, y) within `window` and over `reach` of the
    way to the nearest enemy start. None when a start is unknown."""
    if tally.home is None or not enemy_homes:
        return None
    for second, x, y in starts:
        own = math.dist(tally.home, (x, y))
        enemy = min(math.dist(home, (x, y)) for home in enemy_homes)
        if second - game_start <= window and own > reach * (own + enemy):
            return True
    return False


def _cannon_rushed(tally: _PlayerTally, enemy_homes: list[tuple[float, float]], game_start: int) -> bool | None:
    """See _CANNON_RUSH_WINDOW_SECONDS: nearer an enemy's start than one's own."""
    return _rushed(tally, tally.cannon_starts, enemy_homes, game_start, _CANNON_RUSH_WINDOW_SECONDS, 0.5)


def _spine_rushed(tally: _PlayerTally, enemy_homes: list[tuple[float, float]], game_start: int) -> bool | None:
    """See _CRAWLER_RUSH_WINDOW_SECONDS."""
    return _rushed(
        tally, tally.crawler_starts, enemy_homes, game_start, _CRAWLER_RUSH_WINDOW_SECONDS, _CRAWLER_RUSH_REACH
    )


def _find_game_start(replay) -> int:
    """Second the battle begins (pick phase ends). The map awards Spray*
    decals to every player at game start; first worker command is backup."""
    first_worker = None
    for e in replay.events:
        name = type(e).__name__
        if name == "UpgradeCompleteEvent" and e.upgrade_type_name.startswith("Spray"):
            return e.second
        if (
            first_worker is None
            and name.endswith("CommandEvent")
            and getattr(e, "ability_name", None) in _WORKER_COMMANDS
        ):
            first_worker = e.second
    if first_worker is not None:
        return first_worker
    return _DEFAULT_PICK_PHASE_SECONDS


def _classify_pick_mode(tallies: dict[int, _PlayerTally], game_start: int) -> str:
    if game_start < _DRAFT_START_THRESHOLD:
        return "blind_random"
    first_previews = [t.preview_times[0] for t in tallies.values() if t.preview_times]
    if len(first_previews) >= 2 and max(first_previews) - min(first_previews) >= _DRAFT_SPREAD_THRESHOLD:
        return "single_draft"
    return "tier_draft"


# Repick (107) / keep (108) buttons: 107 opens a confirmation 108 can decline,
# so the last of the two pressed decides.
_REPICK_BUTTON_CONTROL_ID = 107
_KEEP_BUTTON_CONTROL_ID = 108


def _credit_kill(tallies: dict[int, "_PlayerTally"], event, game_start: int) -> None:
    """Credit a death's build cost to the killer's unit type. Skips deaths with
    no killer (~11% of value, mostly non-combat) and self-kills. See
    docs/kill-attribution.md."""
    victim = event.unit
    killer, killing_player = getattr(event, "killing_unit", None), getattr(event, "killing_player", None)
    if victim is None or killer is None or killing_player is None or event.second < game_start:
        return
    owner = getattr(victim, "owner", None)
    if owner is None or owner == killing_player:
        return  # morphs and self-destructs are not kills
    value = (getattr(victim, "minerals", 0) or 0) + (getattr(victim, "vespene", 0) or 0)
    if value > 0:
        tallies[killing_player.pid].kills_by_unit[_canonical_killer(killer.name)] += value


def _tally_events(replay, game_start: int) -> dict[int, _PlayerTally]:
    tallies: dict[int, _PlayerTally] = defaultdict(_PlayerTally)
    for event in replay.events:
        event_name = type(event).__name__
        if event_name == "PlayerStatsEvent":
            if getattr(event, "player", None) is not None:
                tally = tallies[event.player.pid]
                tally.resources_killed = event.resources_killed
                tally.econ_killed = event.minerals_killed_economy + event.vespene_killed_economy
                tally.tech_killed = event.minerals_killed_technology + event.vespene_killed_technology
                tally.resources_lost = event.resources_lost
                if event.second >= game_start:
                    tally.floated_samples.append(event.minerals_current + event.vespene_current)
            continue
        if event_name.endswith("CommandEvent"):
            # Drop play: unloads from transports (Medivac/WarpPrism/Overlord)
            # and Nydus exits. CC/Bunker unloads are defense, not drops.
            ability = (getattr(event, "ability_name", "") or "").lower()
            if (
                "unload" in ability
                and "commandcenter" not in ability
                and "bunker" not in ability
                and event.second >= game_start
                and getattr(event, "player", None) is not None
            ):
                tallies[event.player.pid].drop_commands += 1
            continue
        if event_name == "DialogControlEvent":
            if (
                event.control_id in (_REPICK_BUTTON_CONTROL_ID, _KEEP_BUTTON_CONTROL_ID)
                and event.second < game_start
                and getattr(event, "player", None) is not None
            ):
                tallies[event.player.pid].last_pick_dialog = event.control_id
            continue
        if event_name == "UnitDiedEvent":
            owner = event.unit.owner
            if owner is not None and event.unit.name in _TOWN_HALLS:
                tally = tallies[owner.pid]
                tally.live_town_halls.discard(event.unit.id)
                if not tally.live_town_halls and event.second >= game_start:
                    tally.lost_all_bases = True
            _credit_kill(tallies, event, game_start)
            continue
        if event_name == "UnitInitEvent":
            # A building begun -- only rush structures matter, and where they went.
            owner = event.unit.owner
            if owner is not None and event.second >= game_start:
                if event.unit.name == "PhotonCannon":
                    tallies[owner.pid].cannon_starts.append((event.second, event.x, event.y))
                elif event.unit.name in _RUSH_CRAWLERS:
                    tallies[owner.pid].crawler_starts.append((event.second, event.x, event.y))
            continue
        # UnitBornEvent: instantly-created units. UnitDoneEvent: gradually
        # created ones (warp-ins, morph cocoons, archon merges, buildings).
        if event_name == "UnitBornEvent":
            owner = event.unit_controller
        elif event_name == "UnitDoneEvent":
            owner = event.unit.owner
        else:
            continue
        if owner is None:
            continue
        tally = tallies[owner.pid]
        raw = event.unit.name
        if raw in _WORKER_RACE and event.second >= game_start:
            tally.worker_races[_WORKER_RACE[raw]] += 1
            continue
        if raw in _TOWN_HALLS:
            tally.live_town_halls.add(event.unit.id)
            if event_name == "UnitBornEvent" and tally.home is None:
                tally.home = (event.x, event.y)  # the map-spawned starting base
        if raw in _BASE_STRUCTURES or (raw == "OrbitalCommand" and event_name == "UnitBornEvent"):
            tally.base_times.append(event.second)
        if raw == "OrbitalCommand":
            # Born = the map-spawned starting base; Done = a CC morph
            # completing. Lift-off/landing type changes don't come this way.
            tally.orbitals += 1
        if raw in _HATCHERIES:
            tally.hatcheries += 1
            continue
        if raw == "AutoTurret":
            tally.auto_turrets += 1
            continue
        if raw in _STATIC_DEFENSE:
            if event.second >= game_start:
                tally.static_defense += 1
            continue
        unit = _canonical(raw)
        if unit is None or event.unit.is_building:
            continue
        if event.second < game_start:
            tally.previews.append(unit)
            tally.preview_times.append(event.second)
        else:
            if unit == "Queen":
                if tally.first_queen_second is None:
                    tally.first_queen_second = event.second
            elif tally.first_unit_second is None:
                tally.first_unit_second = event.second
            tally.units[unit] += 1
    return tallies


# Leads smaller than this count as even. Keeps 5+ lead changes (Back-and-forth)
# at ~2.5% of games; 1,000 fires on 9%.
_LEAD_NOISE = 1500


def _game_swings(replay, winning_team: int | None, game_start: int) -> tuple[int | None, int]:
    """(winner's worst deficit or None, lead changes), from value on the map
    (alive + in production). Not kill value or bank: both turned stomps into
    fake comebacks over 1,259 games."""
    team_of = {p.pid: t.number for t in replay.teams for p in t.players}
    first_team = min(t.number for t in replay.teams)
    latest: dict[int, tuple[int, int]] = {}  # player id -> (team, value on the map)
    diffs = []
    for event in replay.events:
        if type(event).__name__ != "PlayerStatsEvent" or getattr(event, "player", None) is None:
            continue
        pid = event.player.pid
        if pid not in team_of:
            continue
        on_map = event.resources_used_current + event.minerals_used_in_progress + event.vespene_used_in_progress
        latest[pid] = (team_of[pid], on_map)
        if event.second < game_start:
            continue  # the pick phase: starting bases only, nothing to lead in
        lead = sum(v for tm, v in latest.values() if tm == first_team) - sum(
            v for tm, v in latest.values() if tm != first_team
        )
        diffs.append(lead)

    state, changes = 0, 0
    for d in diffs:
        sign = 1 if d > _LEAD_NOISE else (-1 if d < -_LEAD_NOISE else 0)
        if sign != 0:
            if state != 0 and sign != state:
                changes += 1
            state = sign
    max_deficit = None
    if winning_team is not None and diffs:
        winner_view = diffs if winning_team == first_team else [-d for d in diffs]
        max_deficit = max(0, -min(winner_view))
    return max_deficit, changes


def _infer_winner(replay) -> tuple[int | None, float, str]:
    """Returns (winning_team, confidence, method)."""
    if replay.winner is not None:
        return replay.winner.number, _CONF_RECORDED, "recorded"

    team_of = {p.pid: team.number for team in replay.teams for p in team.players}
    team_numbers = [t.number for t in replay.teams]

    # Signal 1: final army value per team.
    last_stats = {}
    for e in replay.events:
        if type(e).__name__ == "PlayerStatsEvent" and getattr(e, "player", None) is not None:
            last_stats[e.player.pid] = e
    army = dict.fromkeys(team_numbers, 0)
    for pid, e in last_stats.items():
        if pid in team_of:
            army[team_of[pid]] += e.minerals_used_active_forces + e.vespene_used_active_forces
    army_pick = None
    ranked = sorted(army, key=lambda t: army[t], reverse=True)
    if (
        len(ranked) >= 2
        and army[ranked[0]] >= _ARMY_MINIMUM
        and army[ranked[0]] >= _ARMY_DOMINANCE_RATIO * max(army[ranked[1]], 1)
    ):
        army_pick = ranked[0]

    # Collect gg-chat times and every leave event.
    gg_at = {}
    leaves = []
    for e in replay.events:
        n = type(e).__name__
        if n == "ChatEvent" and getattr(e, "player", None) is not None:
            if _GG_RE.search(e.text.lower()) and e.player.pid in team_of:
                gg_at[e.player.pid] = e.second
        elif n == "PlayerLeaveEvent" and getattr(e, "player", None) is not None:
            if e.player.pid in team_of:
                leaves.append((e.second, e.player.pid, team_of[e.player.pid]))
    leaves.sort()
    # In event (game) time, not game_length's real time.
    end_second = replay.events[-1].second if replay.events else 0

    picks: dict[str, int] = {}

    # Signal 2: gg-concession — a "gg" said shortly before leaving is an
    # explicit concession; a one-sided one names the loser.
    gg_leave_teams = {team for sec, pid, team in leaves if pid in gg_at and sec - gg_at[pid] <= _GG_WINDOW_SECONDS}
    if len(gg_leave_teams) == 1:
        gg_loser = gg_leave_teams.pop()
        picks["gg"] = next(t for t in team_numbers if t != gg_loser)

    # Signal 3: the team that lost more players (not counting the recorder's
    # final leave) conceded.
    if leaves:
        counting = leaves[:-1] if end_second - leaves[-1][0] <= _RECORDER_LEAVE_WINDOW else leaves
        left: dict[int, set] = defaultdict(set)
        for _, pid, team in counting:
            left[team].add(pid)
        gone = sorted(team_numbers, key=lambda t: len(left[t]), reverse=True)
        if len(gone) >= 2 and len(left[gone[0]]) > len(left[gone[1]]):
            picks["departures"] = next(t for t in team_numbers if t != gone[0])

    if army_pick is not None:
        picks["army"] = army_pick

    if not picks:
        return None, 0.0, "unknown"
    winners = set(picks.values())
    method = "inferred:" + "+".join(sorted(picks))
    if len(winners) == 1:
        winner = winners.pop()
        if len(picks) >= 2:
            return winner, _CONF_AGREEMENT, method
        return winner, _CONF_SOLO[next(iter(picks))], method
    # Conflict: gg decides; else departures beat army (base races leave the
    # loser a big idle army).
    if "gg" in picks:
        return picks["gg"], _CONF_GG_CONFLICT, method + "(gg-conflict)"
    if "departures" in picks:
        return picks["departures"], _CONF_DEPARTURES_CONFLICT, method + "(departures-over-army)"
    return None, _CONF_CONFLICT, "conflict:" + "+".join(sorted(picks))


def _remake_called(replay, game_start: int) -> bool:
    """Anyone called for a remake after the battle began (_REMAKE_RE)."""
    lobby = {p.pid for team in replay.teams for p in team.players}
    return any(
        type(e).__name__ == "ChatEvent"
        and e.second >= game_start
        and getattr(e, "player", None) is not None
        and e.player.pid in lobby
        and _REMAKE_RE.search(e.text.lower())
        for e in replay.events
    )


def _game_seed(replay) -> int | None:
    return replay.raw_data["replay.initData"]["game_description"].get("random_value")


def read_game_seed(path: str) -> int | None:
    """Just the game's random seed: the init data, without parsing events."""
    return _game_seed(sc2reader.load_replay(path, load_level=1))


def parse_replay(path: str) -> MonobattleMatch:
    replay = sc2reader.load_replay(path, load_level=4)
    game_start = _find_game_start(replay)
    tallies = _tally_events(replay, game_start)

    pick_mode = _classify_pick_mode(tallies, game_start)
    homes = {(team.number, p.pid): tallies[p.pid].home for team in replay.teams for p in team.players}
    players = []
    for team in replay.teams:
        for p in team.players:
            tally = tallies[p.pid]
            race = tally.race(fallback=p.play_race)
            # Blind-random repick: a second preview, a final "repick" press, or a
            # last preview of the wrong race. Draft previews are just browsing.
            repick_used = repick_from = None
            if pick_mode == "blind_random":
                race_mismatch = bool(tally.previews) and _UNIT_RACE.get(tally.previews[-1], race) != race
                repick_used = (
                    len(tally.previews) > 1 or tally.last_pick_dialog == _REPICK_BUTTON_CONTROL_ID or race_mismatch
                )
                if repick_used and tally.previews:
                    repick_from = tally.previews[0]  # the unit they gave up
            pick = _detect_pick(tally, race)
            # Queens only stop the "bases before your first unit" clock for
            # the player whose pick they are.
            clocks = [tally.first_unit_second]
            if pick == "Queen":
                clocks.append(tally.first_queen_second)
            cutoff = min((s for s in clocks if s is not None), default=None)
            enemy_homes = [home for (t, _), home in homes.items() if t != team.number and home is not None]
            players.append(
                MatchPlayer(
                    name=p.name,
                    toon_handle=getattr(p, "toon_handle", "") or "",
                    team=team.number,
                    race=race,
                    pick=pick,
                    repick_used=repick_used,
                    repick_from=repick_from,
                    resources_killed=tally.resources_killed,
                    econ_killed=tally.econ_killed,
                    tech_killed=tally.tech_killed,
                    resources_lost=tally.resources_lost,
                    resources_floated=(
                        int(statistics.median(tally.floated_samples)) if tally.floated_samples else None
                    ),
                    kills_by_unit=dict(tally.kills_by_unit),
                    drop_commands=tally.drop_commands,
                    static_defense=tally.static_defense,
                    bases_before_unit=sum(1 for s in tally.base_times if cutoff is None or s < cutoff),
                    orbitals=tally.orbitals,
                    lost_all_bases=tally.lost_all_bases,
                    cannon_rush=_cannon_rushed(tally, enemy_homes, game_start),
                    spine_rush=_spine_rushed(tally, enemy_homes, game_start),
                    unit_counts=dict(tally.units),
                )
            )

    winning_team, confidence, method = _infer_winner(replay)
    comeback_deficit, lead_changes = _game_swings(replay, winning_team, game_start)
    return MonobattleMatch(
        file_name=os.path.basename(path),
        map_name=replay.map_name,
        map_hash=replay.map_hash,
        played_at=replay.start_time,
        game_seed=_game_seed(replay),
        duration_seconds=replay.game_length.seconds,
        game_type=replay.real_type,
        pick_mode=pick_mode,
        pick_phase_seconds=game_start,
        players=players,
        winning_team=winning_team,
        winner_confidence=confidence,
        winner_method=method,
        comeback_deficit=comeback_deficit,
        lead_changes=lead_changes,
        remake_called=_remake_called(replay, game_start),
    )


# -- map versions ---------------------------------------------------------
# The terrain name is prose in the published header, e.g.
#     Current Map: The Ashen Cradle <c val="80FFFF">(Created by KillerSmile)</c>
# Formatting varies, so parse leniently; unrecognized means unknown.
_CURRENT_MAP_RE = re.compile(rb"Current Map:\s*([^\x00\x15\r\n]{1,200})")
_TAG_RE = re.compile(r"<[^>]*>")
_AUTHOR_RE = re.compile(r"^(?P<name>.+?)\s*\(\s*(?:created |made )?by\s+(?P<author>[^)]+)\)\s*$", re.IGNORECASE)

# Published maps live in a per-region depot. Matches store only the hash, so
# try the regions the community actually plays on, cheapest first.
_DEPOT_REGIONS = ("us", "eu", "kr")
_DEPOT_URL = "https://{region}-s2-depot.classic.blizzard.com/{map_hash}.s2ma"


def _parse_current_map(header: bytes) -> tuple[str, str | None] | None:
    """Terrain name and author, preferring the copy that credits one."""
    best: tuple[str, str | None] | None = None
    for raw in _CURRENT_MAP_RE.findall(header):
        # Cut at the first line-break tag: the description continues into
        # unrelated prose ("<n/><n/>Join us on discord: ...").
        text = _TAG_RE.sub("", raw.decode("utf8", "replace").split("<n/>")[0]).strip()
        if not text:
            continue
        author = None
        credited = _AUTHOR_RE.match(text)
        if credited:
            text, author = credited.group("name").strip(), credited.group("author").strip()
        if not text:
            continue
        if best is None or (best[1] is None and author is not None):
            best = (text, author)
    return best


def fetch_map_version(map_hash: str) -> MapVersion | None:
    """Download the map behind map_hash and read its terrain (network; cache
    by hash). None on failure."""
    for region in _DEPOT_REGIONS:
        url = _DEPOT_URL.format(region=region, map_hash=map_hash)
        try:
            archive = sc2reader.load_map(url).archive
        except Exception:
            logger.debug("No map %s in the %s depot", map_hash[:12], region, exc_info=True)
            continue
        try:
            found = _parse_current_map(archive.read_file(b"DocumentHeader"))
        except Exception:
            logger.warning("Could not read the header of map %s", map_hash[:12], exc_info=True)
            return None
        if found is None:
            logger.info("Map %s names no current map in its header", map_hash[:12])
            return None
        name, author = found
        return MapVersion(map_hash=map_hash, name=name, author=author)
    logger.info("Map %s not found in any depot", map_hash[:12])
    return None
