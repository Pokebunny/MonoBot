import glob
import os

import pytest
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
        assert m.winner_method == "inferred:army+gg+leaver"
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
