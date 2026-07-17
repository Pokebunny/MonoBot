"""Parse StarCraft II monobattle replays into MonobattleMatch models.

sc2reader is isolated behind this module: nothing else in the bot should
import it, so the parsing backend can be swapped without touching callers.

Pick detection: during the ~60s pick phase the map spawns one preview unit
each time a player selects an option, so the player's final pick is the last
preview unit born before the phase ends — reliable even in games too short
for any production. Production counts corroborate it and act as fallback.

Winner detection: the replay records a winner only when the losing players
all leave before the recorder does. Otherwise we infer from independent
signals — final army value per team, gg-concessions (gg then leave), and
first-leaver order — scoring confidence by their agreement, each signal
blind-validated against replays with recorded winners.
"""

import logging
import os
import re
from collections import Counter, defaultdict

import sc2reader
from models.replay import MatchPlayer, MonobattleMatch

logger = logging.getLogger(__name__)

# Fallback pick-phase length when no game-start marker is found. The real
# boundary is detected per game: the map awards Spray* decals to every player
# the moment the battle starts (workers spawn a second later).
_DEFAULT_PICK_PHASE_SECONDS = 60

# Pick-mode classification: blind random games start at ~63-76s; both draft
# modes spend minutes in the pick phase first.
_DRAFT_START_THRESHOLD = 120
# Single draft picks come one at a time (first previews staggered over the
# whole phase); tier drafts cluster everyone's previews late and close.
_DRAFT_SPREAD_THRESHOLD = 60

_WORKER_COMMANDS = {"TrainProbe", "TrainSCV", "MorphDrone"}

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

# Support units every player has access to regardless of pick. Only treated
# as the pick when auxiliary evidence says so (or nothing else was made):
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

# Units built by morphing a precursor (the pick when meaningfully produced):
# Baneling/Ravager/Lurker/BroodLord from zerg units, Archon from either
# templar type (both templar can merge, but only when Archon is the pick).
# In production fallback the morph only overrides its own precursor — e.g.
# a Mutalisk player's handful of Overseers must not override Mutalisk.
_MORPH_PRECURSORS = {
    "Baneling": {"Zergling"},
    "Ravager": {"Roach"},
    "Lurker": {"Hydralisk"},
    "BroodLord": {"Corruptor"},
    "Archon": {"HighTemplar", "DarkTemplar"},
}

_HATCHERIES = {"Hatchery", "Lair", "Hive"}

# Worker births are the ground truth for the race a player actually played
# (lobby race data can't be trusted in monobattles).
_WORKER_RACE = {"Drone": "Zerg", "Probe": "Protoss", "SCV": "Terran"}

# Race of each pickable unit (canonical names). A preview unit whose race
# doesn't match the race the player actually played is a stale browse (the
# player switched pick at the last moment and the final preview was missed),
# so it must be discarded in favor of production evidence.
_UNIT_RACE = {
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

# Winner-inference tuning. Solo confidences reflect blind validation against
# the 446 replays with a recorded winner (2026-07, event-time clock):
#   army dominance 99.5%, gg-concession 95.8%,
#   first leaver <=5min before recording end 98.8%, earlier only 87.6%
#   (a team playing a man down still wins ~1 in 8).
_ARMY_DOMINANCE_RATIO = 1.5  # team army value ratio considered decisive
_ARMY_MINIMUM = 2000  # leader must have real army or the snapshot is noise
_GG_WINDOW_SECONDS = 90  # gg this close before leaving = concession
# Standalone gg/ggs/ggwp/ggggg — not the "gg" inside "laggin" or "buggy".
_GG_RE = re.compile(r"(?<![a-z])g{2,}\s*(wp)?s?(?![a-z])")
_MIN_CONCESSION_SECOND = 120  # earlier leavers are dropouts, not concessions
_LATE_LEAVE_WINDOW = 300  # first leave this close to recording end = loss
_CONF_RECORDED = 1.0
_CONF_AGREEMENT = 0.9
_CONF_SOLO = {"army": 0.7, "gg": 0.75, "leaver": 0.75, "early-leaver": 0.55}
_CONF_GG_CONFLICT = 0.7  # gg wins conflicts: army value misses base races
_CONF_CONFLICT = 0.0


def _canonical(name: str) -> str | None:
    """Normalize a raw unit-type name; None if it's noise."""
    if name.startswith(_NOISE_PREFIXES) or name in _NOISE_UNITS:
        return None
    if name.endswith("Burrowed"):
        name = name.removesuffix("Burrowed")
    return _NORMALIZE.get(name, name)


class _PlayerTally:
    """Per-player production evidence accumulated from replay events."""

    def __init__(self):
        self.previews: list[str] = []  # pick-phase preview units, in order
        self.preview_times: list[int] = []  # seconds, parallel to previews
        self.units = Counter()  # post-pick-phase army production
        self.hatcheries = 0
        self.auto_turrets = 0
        self.worker_races = Counter()  # race evidence from worker births

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
    # A morph pick implies mass precursor production (e.g. Baneling players
    # born-count mostly Zerglings), so meaningful morph volume overrides
    # its precursor.
    for morph, precursors in _MORPH_PRECURSORS.items():
        if pick in precursors and pool.get(morph, 0) >= 3:
            pick = morph
    return pick


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


def _classify_pick_mode(tallies: dict[str, _PlayerTally], game_start: int) -> str:
    if game_start < _DRAFT_START_THRESHOLD:
        return "blind_random"
    first_previews = [t.preview_times[0] for t in tallies.values() if t.preview_times]
    if len(first_previews) >= 2 and max(first_previews) - min(first_previews) >= _DRAFT_SPREAD_THRESHOLD:
        return "single_draft"
    return "tier_draft"


def _tally_events(replay, game_start: int) -> dict[str, _PlayerTally]:
    tallies: dict[str, _PlayerTally] = defaultdict(_PlayerTally)
    for event in replay.events:
        event_name = type(event).__name__
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
        tally = tallies[owner.name]
        raw = event.unit.name
        if raw in _WORKER_RACE and event.second >= game_start:
            tally.worker_races[_WORKER_RACE[raw]] += 1
            continue
        if raw in _HATCHERIES:
            tally.hatcheries += 1
            continue
        if raw == "AutoTurret":
            tally.auto_turrets += 1
            continue
        unit = _canonical(raw)
        if unit is None or event.unit.is_building:
            continue
        if event.second < game_start:
            tally.previews.append(unit)
            tally.preview_times.append(event.second)
        else:
            tally.units[unit] += 1
    return tallies


def _infer_winner(replay) -> tuple[int | None, float, str]:
    """Returns (winning_team, confidence, method)."""
    if replay.winner is not None:
        return replay.winner.number, _CONF_RECORDED, "recorded"

    team_of = {p.name: team.number for team in replay.teams for p in team.players}
    team_numbers = [t.number for t in replay.teams]

    # Signal 1: final army value per team.
    last_stats = {}
    for e in replay.events:
        if type(e).__name__ == "PlayerStatsEvent" and getattr(e, "player", None) is not None:
            last_stats[e.player.name] = e
    army = dict.fromkeys(team_numbers, 0)
    for name, e in last_stats.items():
        if name in team_of:
            army[team_of[name]] += e.minerals_used_active_forces + e.vespene_used_active_forces
    army_pick = None
    ranked = sorted(army, key=lambda t: army[t], reverse=True)
    if (
        len(ranked) >= 2
        and army[ranked[0]] >= _ARMY_MINIMUM
        and army[ranked[0]] >= _ARMY_DOMINANCE_RATIO * max(army[ranked[1]], 1)
    ):
        army_pick = ranked[0]

    # Signal 2: gg-concession — a "gg" said shortly before leaving is an
    # explicit concession; one-sided concessions name the loser.
    # Signal 3: first leaver — the first non-dropout leaver's team lost;
    # strong when the leave is near the recording's end, weak when the team
    # played on a player down (they win those ~15% of the time).
    gg_at = {}
    leaves = []
    for e in replay.events:
        n = type(e).__name__
        if n == "ChatEvent" and getattr(e, "player", None) is not None:
            if _GG_RE.search(e.text.lower()) and e.player.name in team_of:
                gg_at[e.player.name] = e.second
        elif n == "PlayerLeaveEvent" and getattr(e, "player", None) is not None:
            name = e.player.name
            if name in team_of and e.second >= _MIN_CONCESSION_SECOND:
                leaves.append((e.second, name, team_of[name]))

    gg_leave_teams = {team for sec, name, team in leaves if name in gg_at and sec - gg_at[name] <= _GG_WINDOW_SECONDS}
    # Recording end in EVENT time: event timestamps run on a different clock
    # than game_length (real time vs game time, ~1.4x on Faster), so the end
    # marker must come from the events themselves.
    end_second = replay.events[-1].second if replay.events else 0
    picks: dict[str, int] = {}
    if len(gg_leave_teams) == 1:
        gg_loser = gg_leave_teams.pop()
        picks["gg"] = next(t for t in team_numbers if t != gg_loser)
    if leaves:
        first_second, _, first_team = leaves[0]
        signal = "leaver" if end_second - first_second <= _LATE_LEAVE_WINDOW else "early-leaver"
        picks[signal] = next(t for t in team_numbers if t != first_team)
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
    # Disagreement: an explicit gg concession outranks army value (base
    # races) and leave order; other conflicts go to manual confirmation.
    if "gg" in picks:
        return picks["gg"], _CONF_GG_CONFLICT, method + "(gg-conflict)"
    return None, _CONF_CONFLICT, "conflict:" + "+".join(sorted(picks))


def parse_replay(path: str) -> MonobattleMatch:
    replay = sc2reader.load_replay(path, load_level=4)
    game_start = _find_game_start(replay)
    tallies = _tally_events(replay, game_start)

    pick_mode = _classify_pick_mode(tallies, game_start)
    players = []
    for team in replay.teams:
        for p in team.players:
            tally = tallies[p.name]
            race = tally.race(fallback=p.play_race)
            # In blind random a second preview unit means the player used
            # their repick; in drafts previews are just browsing.
            repick_used = len(tally.previews) > 1 if pick_mode == "blind_random" else None
            players.append(
                MatchPlayer(
                    name=p.name,
                    toon_handle=getattr(p, "toon_handle", "") or "",
                    team=team.number,
                    race=race,
                    pick=_detect_pick(tally, race),
                    repick_used=repick_used,
                    unit_counts=dict(tally.units),
                )
            )

    winning_team, confidence, method = _infer_winner(replay)
    return MonobattleMatch(
        file_name=os.path.basename(path),
        map_name=replay.map_name,
        played_at=replay.start_time,
        duration_seconds=replay.game_length.seconds,
        game_type=replay.real_type,
        pick_mode=pick_mode,
        pick_phase_seconds=game_start,
        players=players,
        winning_team=winning_team,
        winner_confidence=confidence,
        winner_method=method,
    )
