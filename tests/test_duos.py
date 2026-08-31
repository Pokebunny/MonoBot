import datetime

import pytest
from cogs.leaderboard import DUO_MIN_GAMES, Leaderboard
from models.replay import MatchPlayer, MonobattleMatch
from services.match_embeds import duo_board
from services.rating import duo_records

BASE = datetime.datetime(2026, 7, 17, tzinfo=datetime.timezone.utc)


def _match(winning_team, team1=None, team2=None, minutes=0, confidence=1.0, duration=900):
    team1 = team1 or ["A1", "A2", "A3", "A4"]
    team2 = team2 or ["B1", "B2", "B3", "B4"]
    players = [
        MatchPlayer(name=n, toon_handle=n, team=t, race="Zerg", pick="Zergling", repick_used=False, unit_counts={})
        for t, names in ((1, team1), (2, team2))
        for n in names
    ]
    return MonobattleMatch(
        file_name="test.SC2Replay",
        map_name="Monobattle LotV - Map Rotation",
        played_at=BASE + datetime.timedelta(minutes=minutes),
        duration_seconds=duration,
        game_type="4v4",
        pick_mode="blind_random",
        pick_phase_seconds=60,
        players=players,
        winning_team=winning_team,
        winner_confidence=confidence,
        winner_method="recorded",
    )


def test_teammates_get_a_record_opponents_do_not():
    records = duo_records([_match(1)])
    assert records[("A1", "A2")].wins == 1
    assert records[("B1", "B2")].losses == 1
    assert ("A1", "B1") not in records


def test_every_pair_on_a_team_is_counted_once():
    records = duo_records([_match(1)])
    # 4 players a side -> 6 pairs each, and no pair is double-counted.
    assert len(records) == 12
    assert all(r.games == 1 for r in records.values())


def test_pair_key_is_sorted_so_either_order_finds_it():
    records = duo_records([_match(1, team1=["Zed", "Al", "A3", "A4"])])
    assert ("Al", "Zed") in records
    assert ("Zed", "Al") not in records


def test_merged_accounts_count_as_one_person():
    # A2 is the same human as Alt; the pair with A1 is one row, not two.
    records = duo_records([_match(1, team1=["A1", "Alt", "A3", "A4"])], {"Alt": "A2"})
    assert records[("A1", "A2")].wins == 1
    assert ("A1", "Alt") not in records


def test_merged_smurf_on_the_same_team_is_not_its_own_pair():
    records = duo_records([_match(1, team1=["A2", "Alt", "A3", "A4"])], {"Alt": "A2"})
    assert not any("A2" in pair and pair.count("A2") > 1 for pair in records)
    assert ("A2", "A3") in records


def test_unrateable_games_are_left_out():
    unrateable = [
        _match(None),
        _match(1, confidence=0.5),
        _match(1, duration=30),
    ]
    assert duo_records(unrateable) == {}


def test_names_track_the_latest_seen():
    records = duo_records([_match(1), _match(1, team1=["A1", "Renamed", "A3", "A4"], minutes=5)], {"Renamed": "A2"})
    assert records[("A1", "A2")].names == ("A1", "Renamed")


def test_expected_wins_start_at_a_coin_flip():
    # Nobody has played, so both sides hold the prior: half a win each.
    duo = duo_records([_match(1)])[("A1", "A2")]
    assert duo.expected_wins == pytest.approx(0.5)
    assert duo.synergy == pytest.approx(0.5)


def test_synergy_shrinks_as_the_ratings_learn_the_pair():
    # Winning together lifts both players' own ratings, which raises what the
    # model expects of them next time — so the 8th win is worth less than the
    # 1st, and synergy grows by less than one win per win.
    records = duo_records([_match(1, minutes=i) for i in range(8)])
    duo = records[("A1", "A2")]
    assert duo.wins == 8 and duo.losses == 0
    assert duo.expected_wins > 4  # more than the coin flips it started from
    assert 0 < duo.synergy < 4


def test_a_losing_pair_can_still_have_positive_synergy():
    # Beating expectation and winning are not the same thing. The B side is
    # rated up first, then A1+A2 face them with two of the beaten players as
    # teammates: they lose three of four and are still ahead of the model.
    strong = ["B1", "B2", "B3", "B4"]
    weak = ["W1", "W2", "W3", "W4"]
    matches = [_match(1, team1=strong, team2=weak, minutes=i) for i in range(8)]
    underdogs = ["A1", "A2", "W1", "W2"]
    matches.append(_match(1, team1=underdogs, team2=strong, minutes=20))
    matches += [_match(2, team1=underdogs, team2=strong, minutes=21 + i) for i in range(3)]
    duo = duo_records(matches)[("A1", "A2")]
    assert (duo.wins, duo.losses) == (1, 3)
    assert duo.win_rate < 0.5
    assert duo.expected_wins < 1  # the model had them dead
    assert duo.synergy > 0


def test_win_rate_and_games():
    duo = duo_records([_match(1, minutes=0), _match(2, minutes=1)])[("A1", "A2")]
    assert (duo.wins, duo.losses, duo.games) == (1, 1, 2)
    assert duo.win_rate == pytest.approx(0.5)


def _rows():
    matches = [_match(1, minutes=i) for i in range(3)]
    return sorted(duo_records(matches).values(), key=lambda d: d.synergy, reverse=True)


def test_board_leads_with_the_duo_rating_by_default():
    rows = _rows()
    embed = duo_board(rows)
    top = embed.description.splitlines()[0]
    assert f"**{rows[0].display_rating}**" in top
    assert "3-0" in top and "100%" in top
    assert "Duo Rating" in embed.title
    assert "!duos raw" in embed.footer.text and "!duos synergy" in embed.footer.text


def test_board_leads_with_win_rate_when_asked():
    embed = duo_board(_rows(), sort="raw")
    assert "**100%**" in embed.description
    assert "Win Rate" in embed.title
    assert "rated " in embed.description  # the rating still rides along


def test_board_leads_with_synergy_when_asked():
    embed = duo_board(_rows(), sort="synergy")
    top = embed.description.splitlines()[0]
    assert top.index("+") < top.index("3-0")
    assert "Chemistry" in embed.title
    assert "does not repeat" in embed.footer.text  # labelled honestly


def test_board_uses_display_names_for_both_halves():
    rows = _rows()
    names = {"A1": "Ava", "A2": "Bo"}
    assert "**Ava** + **Bo**" in duo_board(rows, display_names=names).description


def test_empty_board_says_so():
    assert "No pair" in duo_board([]).description


@pytest.mark.parametrize(
    "query, expected",
    [
        ("", (DUO_MIN_GAMES, "rating")),
        ("30", (30, "rating")),
        ("raw", (DUO_MIN_GAMES, "raw")),
        ("winrate", (DUO_MIN_GAMES, "raw")),
        ("30 raw", (30, "raw")),
        ("Raw 5", (5, "raw")),
        ("synergy", (DUO_MIN_GAMES, "synergy")),
        ("chemistry", (DUO_MIN_GAMES, "synergy")),
        ("rating", (DUO_MIN_GAMES, "rating")),  # asking for the default is fine
        ("raw synergy", (DUO_MIN_GAMES, "synergy")),  # last word wins
        ("0", (1, "rating")),  # a floor of zero would divide by nothing
    ],
)
def test_duo_query_parsing(query, expected):
    assert Leaderboard._parse_duo_query(query) == expected


def test_a_duo_rating_rises_with_wins_and_falls_with_losses():
    winners = duo_records([_match(1, minutes=i) for i in range(6)])[("A1", "A2")]
    losers = duo_records([_match(1, minutes=i) for i in range(6)])[("B1", "B2")]
    assert winners.display_rating > losers.display_rating
    assert winners.ordinal > losers.ordinal


def test_a_fresh_duo_starts_at_the_model_prior():
    duo = duo_records([_match(1)])[("A1", "A2")]
    # One game in, sigma has barely moved off the prior, so the pair is still
    # provisional-looking rather than sitting at a confident rating.
    assert duo.sigma > 6.0


def test_duo_rating_beats_a_weaker_pair_that_won_the_same_number():
    # Both pairs go 4-0, but one did it against a side the ratings had learned
    # to respect. Raw win rate can't tell them apart; the duo rating can.
    strong = ["S1", "S2", "S3", "S4"]
    weak = ["W1", "W2", "W3", "W4"]
    history = [_match(1, team1=strong, team2=weak, minutes=i) for i in range(10)]
    easy = [_match(1, team1=["A1", "A2", "X1", "X2"], team2=weak, minutes=20 + i) for i in range(4)]
    hard = [_match(1, team1=["B1", "B2", "X3", "X4"], team2=strong, minutes=40 + i) for i in range(4)]
    records = duo_records(history + easy + hard)
    easy_duo, hard_duo = records[("A1", "A2")], records[("B1", "B2")]
    assert easy_duo.win_rate == hard_duo.win_rate == 1.0
    assert hard_duo.ordinal > easy_duo.ordinal


def test_display_rating_is_on_the_player_scale():
    # The entity covers two roster slots, so the shown number is halved to be
    # comparable with the two ladder ratings beside the players' names.
    duo = duo_records([_match(1)])[("A1", "A2")]
    assert duo.display_rating == round(duo.ordinal / 2 * 40 + 1000)
