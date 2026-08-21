"""Profiles must survive a season reset.

Ratings are season-scoped but profiles are not: a player who hasn't played
yet in the open season still has a career, so `!profile` has to resolve them
against career history and report the season standing separately.
"""

import datetime
import types

import pytest
from cogs.leaderboard import Leaderboard
from models.replay import MatchPlayer, MonobattleMatch
from services.rating import RatingCache
from services.storage import MatchStore, hash_replay

BASE = datetime.datetime(2026, 7, 17, 12, 0, tzinfo=datetime.timezone.utc)


def _match(index, winning_team=1, played_at=None):
    names = ["A0", "A1", "A2", "A3", "B0", "B1", "B2", "B3"]
    players = [
        MatchPlayer(
            name=n,
            toon_handle=f"h-{n}",
            team=(1 if i < 4 else 2),
            race="Zerg",
            pick="Zergling",
            unit_counts={"Zergling": 100},
        )
        for i, n in enumerate(names)
    ]
    return MonobattleMatch(
        file_name=f"g{index}.SC2Replay",
        map_name="Monobattle LotV - Map Rotation",
        played_at=played_at or BASE + datetime.timedelta(minutes=index),
        duration_seconds=900,
        game_type="4v4",
        pick_mode="blind_random",
        pick_phase_seconds=63,
        players=players,
        winning_team=winning_team,
        winner_confidence=1.0,
        winner_method="recorded",
    )


@pytest.fixture
def cog(tmp_path):
    store = MatchStore(str(tmp_path / "profile.db"))
    for i in range(6):
        store.ingest(_match(i), hash_replay(f"g{i}".encode()))
    client = types.SimpleNamespace(match_store=store, rating_cache=RatingCache(store))
    yield Leaderboard(client)
    store.close()


def test_profile_resolves_before_a_reset(cog):
    resolved = cog._resolve("A0")
    assert resolved is not None
    career, season, _rank, _total, _n = resolved
    assert career.games == 6
    assert season is not None and season.games == 6


def test_profile_still_resolves_after_a_reset(cog):
    """The regression: a season reset emptied the rating book, so profile
    reported 'no rated games' for everyone until they played again."""
    cog.store.start_season("Season 2")
    resolved = cog._resolve("A0")
    assert resolved is not None, "a player with career games must still have a profile"
    career, season, rank, _total, _n = resolved
    assert career.games == 6  # career survives the reset
    assert season is None  # but they have no standing in the new season
    assert rank is None


def test_profile_self_resolution_survives_a_reset(cog):
    cog.store.link_player("42", "A0")
    author = types.SimpleNamespace(id=42)
    assert cog._resolve_self(author) is not None
    cog.store.start_season("Season 2")
    resolved = cog._resolve_self(author)
    assert resolved is not None
    career, season, _rank, _total, _n = resolved
    assert career.games == 6
    assert season is None


def test_season_rating_returns_once_they_play_again(cog):
    cog.store.start_season("Season 2")
    # Played AFTER the boundary — a game dated before it belongs to Season 1
    # no matter when the replay is uploaded, which is the whole point.
    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5)
    cog.store.ingest(_match(100, played_at=later), hash_replay(b"new"))
    career, season, rank, total, _n = cog._resolve("A0")
    assert career.games == 7  # career counts both seasons
    assert season is not None and season.games == 1  # season counts only the new one
    assert rank is None and total == 0  # 1 game is under MIN_RANKED_GAMES


def test_profile_embed_renders_with_no_season_games(cog):
    """The embed must not divide by zero or show a stale rating when the
    player has no games in the open season."""
    cog.store.start_season("Season 2")
    resolved = cog._resolve("A0")
    ctx = types.SimpleNamespace(guild=None, author=None)
    cog.client.get_user = lambda _id: None
    embed = cog._profile_embed(ctx, resolved, "A0")
    fields = {f.name: f.value for f in embed.fields}
    assert any("Season 2" in name for name in fields), fields
    assert "Career" in fields
    assert "6-0" in fields["Career"] or "0-6" in fields["Career"]
    assert any("No games yet" in v for v in fields.values())
