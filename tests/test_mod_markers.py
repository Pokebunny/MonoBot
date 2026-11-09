"""Marker upgrades from the MonoBattle mod (sc2mod/) override the parser's
inference; replays without them must parse exactly as before."""

import glob
import os
import re

import pytest
import sc2reader
from services import replay_parser

REPLAY_DIR = os.path.join(os.path.dirname(__file__), "..", "test_replays")
REPLAYS = sorted(glob.glob(os.path.join(REPLAY_DIR, "*.SC2Replay")))
# No recorded winner: the result comes from inference unless a marker says otherwise.
UNRECORDED = os.path.join(REPLAY_DIR, "Monobattle LotV - Map Rotation (713).SC2Replay")

UpgradeCompleteEvent = type("UpgradeCompleteEvent", (), {})


def _marker(name, pid=0, second=0, count=1):
    e = UpgradeCompleteEvent()
    e.upgrade_type_name, e.pid, e.second, e.count = name, pid, second, count
    return e


def test_unmarked_replays_have_no_markers():
    for path in REPLAYS:
        replay = sc2reader.load_replay(path, load_level=4)
        assert replay_parser._read_markers(replay.events) is None, path


def test_read_markers():
    markers = replay_parser._read_markers(
        [
            _marker("SprayTerran"),  # not ours
            _marker("MB_Version", count=3),
            _marker("MB_Mode_PickThree"),
            _marker("MB_Offer_Marine", pid=1, second=5),
            _marker("MB_Offer_Roach", pid=1, second=5),
            _marker("MB_Pick_Roach", pid=1, second=20),
            _marker("MB_Start", pid=1, second=31),
            _marker("MB_Start", pid=2, second=30),
            _marker("MB_Win", pid=1, second=900),
            _marker("MB_Loss", pid=2, second=900),
        ]
    )
    assert markers.version == 3
    assert markers.mode == "pick_three"
    assert markers.start == 30
    assert markers.picks == {1: "Roach"}
    assert markers.offers[1] == ["Marine", "Roach"]
    assert markers.results == {1: True, 2: False}


@pytest.fixture(scope="module")
def plain():
    return replay_parser.parse_replay(UNRECORDED)


@pytest.fixture
def marked(monkeypatch, plain):
    """Parse 713 as if it had been played with the mod, with the markers
    build(replay, {name: pid}) returns appended; returns (match, plain parse)."""
    load = sc2reader.load_replay

    def parse(build):
        def load_with_markers(path, **kwargs):
            replay = load(path, **kwargs)
            players = {p.name: p.pid for team in replay.teams for p in team.players}
            replay.events.extend(build(replay, players))
            return replay

        monkeypatch.setattr(sc2reader, "load_replay", load_with_markers)
        return replay_parser.parse_replay(UNRECORDED), plain

    return parse


def test_markers_override_inference(marked, plain):
    start = plain.pick_phase_seconds

    def build(replay, players):
        events = [_marker("MB_Version", count=1), _marker("MB_Mode_Blind")]
        for team in replay.teams:
            for p in team.players:
                events.append(_marker("MB_Offer_Viper", pid=p.pid, second=10))
                if team.number == 1:  # team 1 rolls again
                    events.append(_marker("MB_Offer_Marine", pid=p.pid, second=12))
                events.append(_marker("MB_Pick_Viper" if team.number == 2 else "MB_Pick_Marine", pid=p.pid, second=12))
                events.append(_marker("MB_Start", pid=p.pid, second=start))
                # Inference says team 1 won 713; the markers say team 2.
                events.append(_marker("MB_Win" if team.number == 2 else "MB_Loss", pid=p.pid, second=999))
        return events

    match, _ = marked(build)

    assert plain.winning_team == 1 and plain.winner_method.startswith("inferred")
    assert (match.winning_team, match.winner_method, match.winner_confidence) == (2, "marker", 1.0)
    assert match.pick_mode == "blind_random"
    assert match.pick_phase_seconds == plain.pick_phase_seconds
    for p in match.players:
        if p.team == 1:
            assert (p.pick, p.repick_used, p.repick_from) == ("Marine", True, "Viper")
        else:
            assert (p.pick, p.repick_used, p.repick_from) == ("Viper", False, None)


def test_marker_start_ignores_units_born_before_it(marked):
    """On a melee map the mod deletes the map's starting bases and spawns new
    ones at MB_Start; only what's born from then on counts."""

    def build(replay, players):
        return [_marker("MB_Start", pid=pid, second=10_000) for pid in players.values()]

    match, _ = marked(build)
    assert match.pick_phase_seconds == 10_000
    assert all(not p.unit_counts and p.bases_before_unit == 0 for p in match.players)


def test_new_modes_come_from_markers(marked):
    for marker, mode in (("MB_Mode_PickThree", "pick_three"), ("MB_Mode_Captains", "captains_draft")):
        match, _ = marked(lambda r, p, marker=marker: [_marker(marker)])
        assert match.pick_mode == mode


SC2MOD = os.path.join(os.path.dirname(__file__), "..", "sc2mod")


def test_mod_roster_and_markers_match_the_parser():
    """The mod's roster names are MonoBot's unit names, in the same race, and
    every marker the parser reads is defined in the mod's data."""
    from models.replay import UNIT_RACE

    script = open(os.path.join(SC2MOD, "MonoBattle.galaxy"), encoding="utf8").read()
    defined = set(re.findall(r'<CUpgrade id="MB_(\w+)"', open(os.path.join(SC2MOD, "UpgradeData.xml")).read()))
    roster = re.findall(r'MB_AddUnit\(\d+, "(\w+)", "[^"]*", "(\w+)", "[\w-]+"\);', script)
    races = {"Zerg": "Zerg", "Prot": "Protoss", "Terr": "Terran"}
    assert len(roster) == 42
    for name, race in roster:
        assert UNIT_RACE.get(name) == races[race], name
        assert {f"Pick_{name}", f"Offer_{name}", f"Draft_{name}"} <= defined, name
    assert set(replay_parser._MARKER_MODES) | {"Version", "Start", "Win", "Loss"} <= defined
