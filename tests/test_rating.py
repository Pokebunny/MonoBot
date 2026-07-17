import datetime

from models.replay import MatchPlayer, MonobattleMatch
from services.rating import RatingBook


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
