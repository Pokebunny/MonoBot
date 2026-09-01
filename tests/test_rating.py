import datetime

from models.rating import PlayerRating
from models.replay import MatchPlayer, MonobattleMatch
from services.match_embeds import page_count
from services.rating import RatingBook, match_rating_deltas


def _match(winning_team, confidence=1.0, duration=900, team1=None, team2=None):
    team1 = team1 or ["A1", "A2", "A3", "A4"]
    team2 = team2 or ["B1", "B2", "B3", "B4"]
    # toon_handle = name here so tests can key book.ratings on the name; the
    # name/handle distinction is exercised in test_identity.
    players = [
        MatchPlayer(name=n, toon_handle=n, team=t, race="Zerg", pick="Zergling", repick_used=False, unit_counts={})
        for t, names in ((1, team1), (2, team2))
        for n in names
    ]
    return MonobattleMatch(
        file_name="test.SC2Replay",
        map_name="Monobattle LotV - Map Rotation",
        played_at=datetime.datetime(2026, 7, 17, tzinfo=datetime.timezone.utc),
        duration_seconds=duration,
        game_type="4v4",
        pick_mode="blind_random",
        pick_phase_seconds=60,
        players=players,
        winning_team=winning_team,
        winner_confidence=confidence,
        winner_method="recorded",
    )


def test_winners_gain_losers_lose():
    book = RatingBook()
    assert book.rate_match(_match(winning_team=1))
    a1, b1 = book.ratings["A1"], book.ratings["B1"]
    assert a1.mu > b1.mu
    assert a1.wins == 1 and a1.losses == 0
    assert b1.wins == 0 and b1.losses == 1


def test_repeated_wins_increase_ordinal():
    book = RatingBook()
    for _ in range(10):
        book.rate_match(_match(winning_team=1))
    assert book.ratings["A1"].ordinal > book.ratings["B1"].ordinal
    assert book.rated_matches == 10


def test_low_confidence_skipped():
    book = RatingBook()
    assert not book.rate_match(_match(winning_team=1, confidence=0.5))
    assert book.skipped_matches == 1
    assert not book.ratings


def test_short_game_skipped():
    book = RatingBook()
    assert not book.rate_match(_match(winning_team=1, duration=90))


def test_no_winner_skipped():
    book = RatingBook()
    assert not book.rate_match(_match(winning_team=None))


def test_leaderboard_min_games():
    book = RatingBook()
    book.rate_match(_match(winning_team=1))
    book.rate_match(_match(winning_team=1, team1=["A1", "C2", "C3", "C4"]))
    board = book.leaderboard(min_games=2)
    assert [r.name for r in board][:1] == ["A1"]
    assert all(r.games >= 2 for r in board)


def test_ratings_depend_on_play_order_not_input_order():
    # Ratings are order-dependent by PLAY time; from_matches sorts internally,
    # so the input/upload order must not change the result.
    base = datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc)
    games = []
    for i, wt in enumerate((1, 1, 2)):
        g = _match(winning_team=wt).model_copy(update={"played_at": base + datetime.timedelta(days=i)})
        games.append(g)
    forward = RatingBook.from_matches(games)
    reversed_input = RatingBook.from_matches(list(reversed(games)))
    assert forward.ratings["A1"].mu == reversed_input.ratings["A1"].mu
    assert forward.ratings["A1"].sigma == reversed_input.ratings["A1"].sigma


def test_display_rating_and_provisional():
    fresh = PlayerRating(handle="h", name="n", mu=25.0, sigma=25 / 3)
    assert fresh.provisional  # high sigma = still calibrating
    assert fresh.display_rating == round(fresh.ordinal * 40 + 1000)
    settled = PlayerRating(handle="h", name="n", mu=30.0, sigma=4.0, wins=20, losses=5)
    assert not settled.provisional
    assert settled.display_rating > fresh.display_rating


def test_match_rating_deltas_winners_up_losers_down():
    base = datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc)
    # Three games of the same matchup; probe the delta from the last one.
    games = [
        (i + 1, _match(winning_team=1).model_copy(update={"played_at": base + datetime.timedelta(days=i)}))
        for i in range(3)
    ]
    deltas = match_rating_deltas(games, match_id=3)
    assert set(deltas) == {"A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4"}
    for h, (before, after) in deltas.items():
        if h.startswith("A"):
            assert after > before  # winners gain
        else:
            assert after < before  # losers drop


def test_match_rating_deltas_empty_for_unrateable_game():
    base = datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc)
    games = [(1, _match(winning_team=None).model_copy(update={"played_at": base}))]
    assert match_rating_deltas(games, match_id=1) == {}


def test_match_rating_deltas_start_from_default_for_new_players():
    base = datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc)
    games = [(1, _match(winning_team=1).model_copy(update={"played_at": base}))]
    deltas = match_rating_deltas(games, match_id=1)
    # First-ever game: everyone's "before" is the same default display rating.
    befores = {before for before, _ in deltas.values()}
    assert len(befores) == 1


def test_match_rating_deltas_computed_at_chronological_position():
    # A later-played game's delta must reflect ratings as of its own time,
    # regardless of the order matches are passed in.
    base = datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc)
    games = [
        (i + 1, _match(winning_team=1).model_copy(update={"played_at": base + datetime.timedelta(days=i)}))
        for i in range(4)
    ]
    forward = match_rating_deltas(games, match_id=4)
    shuffled = match_rating_deltas(list(reversed(games)), match_id=4)
    assert forward == shuffled


def test_leaderboard_page_count():
    assert page_count([]) == 1
    assert page_count(list(range(20))) == 1
    assert page_count(list(range(21))) == 2
    assert page_count(list(range(45))) == 3


def test_rating_cache_follows_the_open_season(tmp_path):
    """A season turnover changes the window without changing any match, so the
    cache must invalidate on it — not only on change_count."""
    from services.rating import RatingCache
    from services.storage import MatchStore, hash_replay

    store = MatchStore(str(tmp_path / "seasons.db"))
    try:
        base = datetime.datetime(2026, 7, 17, tzinfo=datetime.timezone.utc)
        for i in range(4):
            m = _match(winning_team=1)
            m.played_at = base + datetime.timedelta(minutes=i)
            m.file_name = f"g{i}.SC2Replay"
            store.ingest(m, hash_replay(f"g{i}".encode()))

        cache = RatingCache(store)
        assert cache.book().leaderboard(min_games=1)
        cached = cache.book()
        assert cache.book() is cached  # no write, no rebuild

        store.start_season("Season 2")
        assert cache.book() is not cached
        assert cache.book().leaderboard(min_games=1) == []

        # career=True ignores the season window entirely
        career = RatingCache(store, career=True)
        assert career.book().leaderboard(min_games=1)
    finally:
        store.close()


def test_battle_seconds_excludes_the_pick_phase():
    match = _match(1, duration=900)
    match.pick_phase_seconds = 63  # a blind-random draft
    assert match.battle_seconds == 837


def test_battle_seconds_never_goes_negative():
    # A handful of games end during the draft itself.
    match = _match(1, duration=120)
    match.pick_phase_seconds = 204
    assert match.battle_seconds == 0


def test_match_summary_shows_battle_time_not_replay_length():
    from services.match_embeds import match_summary

    match = _match(1, duration=900)
    match.pick_phase_seconds = 240  # a drafted game: four minutes of picking
    assert "11:00" in match_summary(match).description
    assert "15:00" not in match_summary(match).description
