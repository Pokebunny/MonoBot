import glob
import os
import statistics

import pytest
from models.replay import STATIC_DEFENSE_KILLERS, WORKER_KILLERS
from services import replay_parser

REPLAY_DIR = os.path.join(os.path.dirname(__file__), "..", "test_replays")
REPLAYS = sorted(glob.glob(os.path.join(REPLAY_DIR, "*.SC2Replay")))

pytestmark = pytest.mark.skipif(not REPLAYS, reason="no test replays available")


@pytest.fixture(scope="module")
def matches():
    return {os.path.basename(p): replay_parser.parse_replay(p) for p in REPLAYS}


def _match(matches, number):
    return matches[f"Monobattle LotV - Map Rotation ({number}).SC2Replay"]


def test_basic_metadata(matches):
    m = _match(matches, 714)
    assert m.map_name == "Monobattle LotV - Map Rotation"
    # Every game shares that arcade name; the hash is what says which terrain
    # was published at the time (see services/map_versions.py).
    assert len(m.map_hash) == 64
    assert m.game_type == "4v4"
    assert len(m.players) == 8
    assert len(m.team(1)) == 4
    assert len(m.team(2)) == 4
    assert 14 * 60 < m.duration_seconds < 18 * 60


def test_recorded_winner(matches):
    m = _match(matches, 714)
    assert m.winning_team == 2
    assert m.winner_confidence == 1.0
    assert m.winner_method == "recorded"


def test_inferred_winner(matches):
    # 713 and 716 have no recorded winner; the losing team gg'd out near the
    # end and army value agrees, all signals pointing at team 1.
    for number in (713, 716):
        m = _match(matches, number)
        assert m.winning_team == 1, m.file_name
        assert m.winner_method == "inferred:army+departures+gg"
        assert m.winner_confidence == 0.9


def test_all_picks_detected(matches):
    for m in matches.values():
        for p in m.players:
            assert p.pick is not None, f"{m.file_name}: {p.name} has no pick"


def test_known_picks(matches):
    picks = {p.name: p.pick for p in _match(matches, 714).players}
    assert picks == {
        "QuebecJay": "Immortal",
        "AbellaDanger": "DarkTemplar",
        "Mrumpa": "Mutalisk",
        "BenZenZ": "Cyclone",
        "HODOR": "Stalker",
        "Slug": "Carrier",
        "Magnath": "Hydralisk",
        "Pokebunny": "Zergling",
    }


def test_pick_mode_blind_random(matches):
    for m in matches.values():
        assert m.pick_mode == "blind_random", m.file_name
        assert 55 <= m.pick_phase_seconds <= 90, m.file_name


def test_repick_detection(matches):
    repicks = {p.name: p.repick_used for p in _match(matches, 714).players}
    assert repicks == {
        "QuebecJay": True,  # SiegeTank -> Immortal
        "AbellaDanger": True,  # SwarmHost -> DarkTemplar
        "Mrumpa": False,
        "BenZenZ": False,
        "HODOR": True,  # Infestor -> Stalker
        "Slug": True,  # HighTemplar -> Carrier
        "Magnath": False,
        "Pokebunny": True,  # Baneling -> Zergling
    }


def test_morph_variants_normalized(matches):
    # 715: two Thor players (born as ThorAP), one Adept (AdeptPhaseShift noise)
    picks = {p.name: p.pick for p in _match(matches, 715).players}
    assert picks["AbellaDanger"] == "Thor"
    assert picks["Pokebunny"] == "Thor"
    assert picks["HODOR"] == "Adept"
    # 716: Lurker player (morph from Hydralisk, born as LurkerBurrowed)
    picks = {p.name: p.pick for p in _match(matches, 716).players}
    assert picks["BenZenZ"] == "Lurker"


def test_last_second_repick_detected(matches):
    """Replay 721: NecesaryPapr hit the repick button at ~61s of a 70s pick
    phase, so the new preview unit never spawned. The button click itself
    must count as the repick, and the abandoned unit is recorded."""
    m = _match(matches, 721)
    papr = next(p for p in m.players if p.name == "NecesaryPapr")
    assert papr.repick_used is True
    assert papr.repick_from == "Battlecruiser"
    assert papr.pick == "Sentry"
    # ordinary two-preview repicks record their original unit too
    pokebunny = next(p for p in m.players if p.name == "Pokebunny")
    assert pokebunny.repick_used is True
    assert pokebunny.repick_from == "Corruptor"
    assert pokebunny.pick == "Carrier"
    # and non-repickers stay untouched
    slug = next(p for p in m.players if p.name == "Slug")
    assert slug.repick_used is False
    assert slug.repick_from is None


def test_mvp_from_kill_stats(matches):
    """Replay 721: Pokebunny's Carriers destroyed by far the most value on
    the winning team."""
    m = _match(matches, 721)
    for p in m.players:
        assert p.resources_killed is not None and p.resources_killed >= 0
    mvp = m.mvp()
    assert mvp.name == "Pokebunny"
    assert mvp.resources_killed > 30000


# -- kill attribution ----------------------------------------------------


def test_kills_by_unit_tracks_the_official_total(matches):
    """Summed per player, attributed value reproduces the game's own
    resources_killed -- in the CENTRE of the distribution. Measured over 300
    replays: median 1.00, but a 5% tail lands under 0.70 (deaths the tracker
    credits to nobody, concentrated in expensive air kills). So this pins the
    median tightly and each individual only loosely; see
    docs/kill-attribution.md before tightening either bound."""
    ratios = [
        sum(p.kills_by_unit.values()) / p.resources_killed
        for m in matches.values()
        for p in m.players
        if p.resources_killed and p.resources_killed >= 5000
    ]
    assert ratios
    assert 0.90 < statistics.median(ratios) < 1.10
    assert all(0.3 < r < 1.5 for r in ratios), sorted(ratios)[:3]


def test_kill_killer_names_are_normalized(matches):
    # A sieged tank is a tank and a burrowed lurker is a lurker: mode variants
    # must collapse the same way production counts do.
    for m in matches.values():
        for p in m.players:
            for unit in p.kills_by_unit:
                assert not unit.endswith("Burrowed"), unit
                assert unit not in ("ThorAP", "SiegeTankSieged", "VikingAssault", "LiberatorAG"), unit


def test_kills_attributed_to_spawned_children(matches):
    # A Carrier never kills anything itself -- its interceptors do -- and the
    # same holds for a Swarm Host's locusts and a Raven's turrets. own_kills
    # has to see through that or those picks read as zero.
    by_pick = {p.pick: p for m in matches.values() for p in m.players if p.kills_by_unit}
    carrier = by_pick.get("Carrier")
    if carrier is not None:
        assert carrier.kills_by_unit.get("Interceptor", 0) > 0
        assert carrier.own_kills >= carrier.kills_by_unit["Interceptor"]
    swarm_host = by_pick.get("SwarmHost")
    if swarm_host is not None:
        assert swarm_host.kills_by_unit.get("Locust", 0) > 0
        assert swarm_host.own_kills >= swarm_host.kills_by_unit["Locust"]


def test_own_kills_excludes_static_defense_and_workers(matches):
    checked = 0
    for m in matches.values():
        for p in m.players:
            extra = sum(v for u, v in p.kills_by_unit.items() if u in STATIC_DEFENSE_KILLERS | WORKER_KILLERS)
            if extra:
                assert p.own_kills <= sum(p.kills_by_unit.values()) - extra
                checked += 1
    assert checked, "no player in the sample got a kill with a cannon or a worker"
