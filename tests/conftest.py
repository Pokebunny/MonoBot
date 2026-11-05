import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "main"))

from models.replay import MatchPlayer, MonobattleMatch  # noqa: E402
from services.storage import MatchStore  # noqa: E402

BASE = datetime.datetime(2026, 7, 17, 12, 0, tzinfo=datetime.UTC)
TEAM1 = ["A1", "A2", "A3", "A4"]
TEAM2 = ["B1", "B2", "B3", "B4"]


def make_player(name, team, handle=None, pick="Zergling", **fields) -> MatchPlayer:
    """toon_handle defaults to the name."""
    fields.setdefault("race", "Zerg")
    fields.setdefault("unit_counts", {})
    return MatchPlayer(name=name, toon_handle=handle or name, team=team, pick=pick, **fields)


def make_match(players=None, *, team1=TEAM1, team2=TEAM2, winning_team=1, confidence=None, method=None, **fields):
    """A rateable 4v4 at BASE unless told otherwise. Without `players`, team1/team2
    name the roster (handle = name)."""
    if players is None:
        players = [make_player(n, t) for t, names in ((1, team1), (2, team2)) for n in names]
    defaults = {
        "file_name": "test.SC2Replay",
        "map_name": "Monobattle LotV - Map Rotation",
        "played_at": BASE,
        "duration_seconds": 900,
        "game_type": "4v4",
        "pick_mode": "blind_random",
        "pick_phase_seconds": 60,
    }
    return MonobattleMatch(
        players=players,
        winning_team=winning_team,
        winner_confidence=(1.0 if winning_team else 0.0) if confidence is None else confidence,
        winner_method=method or ("recorded" if winning_team else "unknown"),
        **(defaults | fields),
    )


def padded_roster(roster):
    """(name, handle) pairs for team 1 then team 2, each side topped up to four
    with bystanders, since only 4v4s are stored."""
    half = len(roster) // 2
    sides = (roster[:half], roster[half:])
    return [
        *sides[0], *((f"Pad1{j}", f"h-pad1{j}") for j in range(4 - len(sides[0]))),
        *sides[1], *((f"Pad2{j}", f"h-pad2{j}") for j in range(4 - len(sides[1]))),
    ]  # fmt: skip


@pytest.fixture
def store(tmp_path):
    s = MatchStore(str(tmp_path / "test.db"))
    yield s
    s.close()


def community_cog(cog_cls, store, **client_attrs):
    """A community cog bound to one community around `store`, as a command
    would see it."""
    from types import SimpleNamespace

    from community import Community
    from models.config import GuildConfig

    community = Community(None, GuildConfig(), store)
    client = SimpleNamespace(communities=SimpleNamespace(for_guild=lambda _guild: community), **client_attrs)
    return cog_cls(client).at(community)
