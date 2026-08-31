import asyncio
import types

import pytest
from cogs.matchmaking import ProposedMatchView
from models.matchmaking import QueuedPlayer
from services.matchmaking import balance_teams, ranked_matches
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


def test_ranked_matches_are_all_distinct_splits_best_first():
    options = ranked_matches([_p(f"p{i}") for i in range(8)])
    assert len(options) == 35  # C(7, 3): every split, mirror-deduped
    gaps = [abs(0.5 - o.team1_win_probability) for o in options]
    assert gaps == sorted(gaps)  # most balanced first
    # The top option matches what balance_teams picks alone.
    assert balance_teams([_p(f"p{i}") for i in range(8)]).team1_win_probability == options[0].team1_win_probability


def test_ranked_matches_limit_caps_the_list():
    options = ranked_matches([_p(f"p{i}") for i in range(8)], limit=8)
    assert len(options) == 8
    # Kept the 8 most balanced, dropped the rest.
    full = ranked_matches([_p(f"p{i}") for i in range(8)])
    assert [o.team1_win_probability for o in options] == [o.team1_win_probability for o in full[:8]]


def test_ranked_matches_single_split_for_a_pair():
    options = ranked_matches([_p("a"), _p("b")])
    assert len(options) == 1  # nothing to shuffle through


class _Response:
    """Records what the view did with the interaction."""

    def __init__(self):
        self.edited = self.deferred = False
        self.message = None

    async def edit_message(self, **kwargs):
        self.edited = True

    async def defer(self):
        self.deferred = True

    async def send_message(self, content, ephemeral=False):
        self.message = content


class _Message:
    def __init__(self):
        self.deleted = False

    async def delete(self):
        self.deleted = True


class _Interaction:
    def __init__(self, user_id="1"):
        self.user = types.SimpleNamespace(id=user_id)
        self.response = _Response()
        self.channel = object()
        self.guild = object()
        self.message = _Message()


class _Store:
    def __init__(self, count=0):
        self.count = count

    def match_count(self):
        return self.count


class _Cog:
    def __init__(self, store, stored_ids=(), resolved=None):
        self.store = store
        self.reposted = None
        self.stored_ids = list(stored_ids)
        self.resolved = list(resolved) if resolved is not None else None

    async def post_match(self, channel, users):
        self.reposted = users

    def stored_roster_ids(self):
        return self.stored_ids

    def resolve_roster(self, guild):
        return list(self.resolved or [])


def _view(store, option_count=3):
    """A posted match between eight players, with `option_count` splits ranked."""
    players = [_p(f"p{i}") for i in range(8)]
    options = ranked_matches(players, limit=option_count)
    users = [types.SimpleNamespace(id=str(i)) for i in range(8)]
    return ProposedMatchView(_Cog(store), users, options), users


class TestNewTeamsButton:
    """One button: cycle the ranked splits until a game is played, re-balance
    from live ratings afterwards."""

    def test_cycles_splits_when_nothing_has_been_played(self):
        view, _ = _view(_Store(count=5))
        interaction = _Interaction()
        asyncio.run(view.new_teams.callback(interaction))
        assert view.index == 1
        assert interaction.response.edited  # edited in place, not reposted
        assert view.cog.reposted is None

    def test_rebalances_once_a_game_is_stored(self):
        store = _Store(count=5)
        view, users = _view(store)
        store.count += 1  # a replay went up while the teams sat there
        interaction = _Interaction()
        asyncio.run(view.new_teams.callback(interaction))
        assert view.cog.reposted == users  # re-split from current ratings
        assert view.index == 0  # the stale options were not cycled

    def test_single_split_and_no_games_says_so(self):
        view, _ = _view(_Store(count=5), option_count=1)
        interaction = _Interaction()
        asyncio.run(view.new_teams.callback(interaction))
        assert "no games have been played since" in interaction.response.message

    def test_only_players_in_the_match_may_re_team(self):
        view, _ = _view(_Store(count=5))
        interaction = _Interaction(user_id="stranger")
        asyncio.run(view.new_teams.callback(interaction))
        assert "Only a player in this match" in interaction.response.message
        assert view.index == 0


class TestRestoredAfterRestart:
    """A deploy restarts the bot under proposals still sitting in chat. The
    view is persistent so the click still dispatches; what it lost is the
    ranked options, so it re-balances the stored roster instead."""

    def test_rebalances_from_the_stored_roster(self):
        users = [types.SimpleNamespace(id=str(i)) for i in range(8)]
        cog = _Cog(_Store(count=5), stored_ids=[str(i) for i in range(8)], resolved=users)
        view = ProposedMatchView(cog)  # no users, no options: restored
        interaction = _Interaction()
        asyncio.run(view.new_teams.callback(interaction))
        assert cog.reposted == users
        assert interaction.message.deleted  # the pre-restart proposal goes away

    def test_says_so_when_the_roster_cannot_be_resolved(self):
        cog = _Cog(_Store(count=5), stored_ids=["1"], resolved=[])
        view = ProposedMatchView(cog)
        interaction = _Interaction()
        asyncio.run(view.new_teams.callback(interaction))
        assert "run `!teams`" in interaction.response.message
        assert cog.reposted is None

    def test_permission_falls_back_to_the_stored_roster(self):
        cog = _Cog(_Store(count=5), stored_ids=["7"], resolved=[])
        view = ProposedMatchView(cog)
        interaction = _Interaction(user_id="stranger")
        asyncio.run(view.new_teams.callback(interaction))
        assert "Only a player in this match" in interaction.response.message
