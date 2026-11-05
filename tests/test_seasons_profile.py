"""Profiles must survive a season reset.

Ratings are season-scoped but profiles are not: a player who hasn't played
yet in the open season still has a career, so `!profile` has to resolve them
against career history and report the season standing separately.
"""

import datetime
import types

import pytest
from cogs.leaderboard import Leaderboard
from conftest import BASE, community_cog, make_match, make_player
from models.rating import PlayerRating
from services import identity, match_embeds
from services.storage import MatchStore, hash_replay


def _match(index, winning_team=1, played_at=None):
    names = ["A0", "A1", "A2", "A3", "B0", "B1", "B2", "B3"]
    players = [
        make_player(n, 1 if i < 4 else 2, handle=f"h-{n}", unit_counts={"Zergling": 100}) for i, n in enumerate(names)
    ]
    return make_match(
        players,
        winning_team=winning_team,
        file_name=f"g{index}.SC2Replay",
        played_at=played_at or BASE + datetime.timedelta(minutes=20 * index),
        pick_phase_seconds=63,
    )


@pytest.fixture
def cog(tmp_path):
    store = MatchStore(str(tmp_path / "profile.db"))
    for i in range(6):
        store.ingest(_match(i), hash_replay(f"g{i}".encode()))
    yield community_cog(Leaderboard, store)
    store.close()


def _resolve(cog, name):
    """What !profile <name> shows, minus Discord."""
    return cog._resolve_person(identity.resolve(cog.store, name)[0])


def test_profile_resolves_before_a_reset(cog):
    resolved = _resolve(cog, "A0")
    assert resolved is not None
    career, season, _rank, _total, _n = resolved
    assert career.games == 6
    assert season is not None and season.games == 6


def test_profile_still_resolves_after_a_reset(cog):
    """The regression: a season reset emptied the rating book, so profile
    reported 'no rated games' for everyone until they played again."""
    cog.store.start_season("Season 2")
    resolved = _resolve(cog, "A0")
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
    career, season, rank, total, _n = _resolve(cog, "A0")
    assert career.games == 7
    assert season is not None and season.games == 1
    assert rank is None and total == 0  # 1 game is under MIN_RANKED_GAMES


def test_profile_embed_renders_with_no_season_games(cog):
    """The embed must not divide by zero or show a stale rating when the
    player has no games in the open season."""
    cog.store.start_season("Season 2")
    resolved = _resolve(cog, "A0")
    ctx = types.SimpleNamespace(guild=None, author=None)
    cog.client.get_user = lambda _id: None
    embed = cog._profile_view(ctx, resolved).profile
    fields = {f.name: f.value for f in embed.fields}
    assert any("Season 2" in name for name in fields), fields
    assert "Career" in fields
    assert "6-0" in fields["Career"] or "0-6" in fields["Career"]
    assert any("No games yet" in v for v in fields.values())


def test_all_units_lists_every_pick_past_the_profiles_top_ten():
    ratings = {f"Unit{i}": PlayerRating(handle="h", name="A0", mu=20 + i, sigma=5, wins=i, losses=1) for i in range(15)}
    embed = match_embeds.player_units("A0", ratings)
    assert embed.description.count("\n") == 14  # all fifteen, one per line
    assert embed.description.startswith("**Unit14**")  # highest rated first
    assert "15 units" in embed.footer.text


def test_profile_shows_career_race_and_unit_ratings(cog):
    resolved = _resolve(cog, "A0")
    ctx = types.SimpleNamespace(guild=None, author=None)
    cog.client.get_user = lambda _id: None
    view = cog._profile_view(ctx, resolved)
    fields = {f.name: f.value for f in view.profile.fields}
    rating = resolved[0]
    boards = cog.pick_boards.boards()  # same walk the profile reads
    zergling = next(r for r in boards["Zergling"] if r.handle == rating.handle)
    assert fields["Races · career"].startswith("**Zerg** — **")
    assert fields["Top-rated units · career"] == f"**Zergling** — **{zergling.display_rating}** · 6-0 (100%)"
    assert view.units.description == fields["Top-rated units · career"]
