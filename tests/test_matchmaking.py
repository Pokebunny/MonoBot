import pytest
from models.matchmaking import QueuedPlayer
from services.matchmaking import balance_teams
from services.rating import DEFAULT_MU, DEFAULT_SIGMA, predict_win_probability


def _p(name, mu=DEFAULT_MU, sigma=DEFAULT_SIGMA):
    return QueuedPlayer(discord_id=name, display_name=name, sc2_name=name, mu=mu, sigma=sigma)


def test_equal_players_balanced():
    match = balance_teams([_p(f"p{i}") for i in range(8)])
    assert len(match.team1) == 4 and len(match.team2) == 4
    assert match.team1_win_probability == pytest.approx(0.5, abs=1e-6)
    assert match.fairness == pytest.approx(1.0, abs=1e-6)


def test_strong_players_split_across_teams():
    # 4 strong, 4 weak: the fair split is 2 strong + 2 weak per side.
    strong = [_p(f"s{i}", mu=40, sigma=2) for i in range(4)]
    weak = [_p(f"w{i}", mu=15, sigma=2) for i in range(4)]
    match = balance_teams(strong + weak)
    strong_names = {p.display_name for p in strong}
    t1_strong = sum(p.display_name in strong_names for p in match.team1)
    assert t1_strong == 2  # not 4v0 stacked
    assert match.team1_win_probability == pytest.approx(0.5, abs=0.05)


def test_balancer_beats_naive_stacking():
    # Balancer's split must be at least as close to 50/50 as stacking all the
    # strong players on one team.
    strong = [_p(f"s{i}", mu=38, sigma=2) for i in range(4)]
    weak = [_p(f"w{i}", mu=18, sigma=2) for i in range(4)]
    match = balance_teams(strong + weak)
    stacked = predict_win_probability([(p.mu, p.sigma) for p in strong], [(p.mu, p.sigma) for p in weak])
    assert abs(0.5 - match.team1_win_probability) < abs(0.5 - stacked)
    assert match.fairness == pytest.approx(1.0 - 2 * abs(0.5 - match.team1_win_probability))


def test_anchor_always_on_team1():
    players = [_p(f"p{i}") for i in range(8)]
    match = balance_teams(players)
    assert players[0] in match.team1


def test_three_v_three():
    match = balance_teams([_p(f"p{i}") for i in range(6)])
    assert len(match.team1) == 3 and len(match.team2) == 3


def test_odd_count_rejected():
    with pytest.raises(ValueError):
        balance_teams([_p(f"p{i}") for i in range(7)])


def test_empty_rejected():
    with pytest.raises(ValueError):
        balance_teams([])
