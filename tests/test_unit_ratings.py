import datetime

from cogs.leaderboard import UNIT_MIN_GAMES, Leaderboard
from models.replay import MatchPlayer, MonobattleMatch
from services.match_embeds import unit_board
from services.rating import unit_baseline, unit_ratings

BASE = datetime.datetime(2026, 7, 17, tzinfo=datetime.timezone.utc)


def _match(winning_team, team1=None, team2=None, picks1=None, picks2=None, minutes=0, confidence=1.0):
    team1 = team1 or ["A1", "A2", "A3", "A4"]
    team2 = team2 or ["B1", "B2", "B3", "B4"]
    picks1 = picks1 or ["Zergling"] * 4
    picks2 = picks2 or ["Roach"] * 4
    players = [
        MatchPlayer(name=n, toon_handle=n, team=t, race="Zerg", pick=pick, repick_used=False, unit_counts={})
        for t, names, picks in ((1, team1, picks1), (2, team2, picks2))
        for n, pick in zip(names, picks)
    ]
    return MonobattleMatch(
        file_name="test.SC2Replay",
        map_name="Monobattle LotV - Map Rotation",
        played_at=BASE + datetime.timedelta(minutes=minutes),
        duration_seconds=900,
        game_type="4v4",
        pick_mode="blind_random",
        pick_phase_seconds=60,
        players=players,
        winning_team=winning_team,
        winner_confidence=confidence,
        winner_method="recorded",
    )


def test_winning_pick_rises_losing_pick_falls():
    units = unit_ratings([_match(1)])
    assert units["Zergling"].mu > units["Roach"].mu
    assert (units["Zergling"].wins, units["Zergling"].losses) == (4, 0)
    assert (units["Roach"].wins, units["Roach"].losses) == (0, 4)


def test_a_pick_fielded_twice_gets_both_slots_moves():
    once = unit_ratings([_match(1, picks1=["Zergling", "Hydralisk", "Mutalisk", "Ultralisk"])])
    twice = unit_ratings([_match(1, picks1=["Zergling", "Zergling", "Mutalisk", "Ultralisk"])])
    assert twice["Zergling"].mu - 25 > once["Zergling"].mu - 25 > 0


def test_unrateable_and_unpicked_games_are_skipped():
    low_confidence = _match(1, confidence=0.5)
    unpicked = _match(1)
    unpicked.players[0].pick = None
    assert unit_ratings([low_confidence, unpicked]) == {}


def test_a_pick_carried_by_strong_players_rates_below_one_that_wins_on_its_own():
    # Both picks go 4-0 in their games, but Carrier only ever won in the hands
    # of a side the ladder had learned was much stronger. Raw win rate can't
    # tell them apart; the unit rating credits the players, not the pick.
    strong = ["S1", "S2", "S3", "S4"]
    weak = ["W1", "W2", "W3", "W4"]
    even = ["E1", "E2", "E3", "E4"]
    history = [
        _match(1, team1=strong, team2=weak, picks1=["Zealot"] * 4, picks2=["Zealot"] * 4, minutes=i) for i in range(10)
    ]
    carried = [
        _match(1, team1=strong, team2=weak, picks1=["Carrier"] * 4, picks2=["Zealot"] * 4, minutes=20 + i)
        for i in range(4)
    ]
    earned = [
        _match(
            1, team1=even, team2=["F1", "F2", "F3", "F4"], picks1=["Stalker"] * 4, picks2=["Zealot"] * 4, minutes=40 + i
        )
        for i in range(4)
    ]
    units = unit_ratings(history + carried + earned)
    assert units["Carrier"].win_rate == units["Stalker"].win_rate == 1.0
    assert units["Stalker"].mu > units["Carrier"].mu


def test_baseline_is_the_games_weighted_average():
    units = unit_ratings([_match(1)])
    baseline = unit_baseline(units.values())
    assert units["Zergling"].points(baseline) == -units["Roach"].points(baseline)


def test_board_leads_with_bonus_and_footer_points_at_raw():
    units = unit_ratings([_match(1)])
    rows = sorted(units.values(), key=lambda u: u.mu, reverse=True)
    embed = unit_board(rows, baseline=unit_baseline(rows))
    assert embed.title == "Unit Ratings"
    assert "**Zergling** — **+" in embed.description
    assert "!leaderboard units raw" in embed.footer.text
    raw = unit_board(rows, baseline=unit_baseline(rows), sort="raw")
    assert "**Zergling** — **100%**" in raw.description


def test_parse_unit_query():
    assert Leaderboard._parse_unit_query("units") == (UNIT_MIN_GAMES, False)
    assert Leaderboard._parse_unit_query("units 30 raw") == (30, True)
    assert Leaderboard._parse_unit_query("Units Winrate") == (UNIT_MIN_GAMES, True)
