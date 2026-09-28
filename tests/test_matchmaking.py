import asyncio
import datetime as dt
import types

import pytest
from cogs.matchmaking import AfkCheckView, Matchmaking, NextGameView, ProposedMatchView
from models.matchmaking import QueuedPlayer
from services.matchmaking import afk_due, balance_teams, next_roster, ranked_matches
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
        self.embeds = []  # no embed to redraw; the lineup is read off the cog

    async def edit(self, **kwargs):
        pass

    async def delete(self):
        self.deleted = True


class _Interaction:
    def __init__(self, user_id="1"):
        self.user = types.SimpleNamespace(id=user_id)
        self.response = _Response()
        self.channel = object()
        self.guild = _Guild()
        self.message = _Message()


class _Guild:
    """Every waitlister is still in the server."""

    def get_member(self, uid):
        return types.SimpleNamespace(id=str(uid))


class _Store:
    def __init__(self, count=0):
        self.count = count

    def match_count(self):
        return self.count

    def sc2_names_for(self, uid):
        return ["linked"]


class _Cog:
    def __init__(self, store, stored_ids=(), resolved=None):
        self.store = store
        self.reposted = None
        self.stored_ids = list(stored_ids)
        self.resolved = list(resolved) if resolved is not None else None
        self.promoted = None
        self.sitting_out = []
        self.waitlist = []
        self.last_roster = []
        self.match_message = self.summary_message = None

    # The real lineup logic, run against this fake's state.
    lineup_for_next = Matchmaking.lineup_for_next
    lineup_embed = Matchmaking.lineup_embed
    toggle_lineup = Matchmaking.toggle_lineup
    re_team = Matchmaking.re_team
    _redraw_lineup = Matchmaking._redraw_lineup

    def save_lineup(self):
        pass

    async def post_match(self, channel, users, promoted=()):
        self.reposted = users
        self.promoted = list(promoted)

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


_T0 = dt.datetime(2026, 9, 27, 20, 0, tzinfo=dt.UTC)
_AFTER = dt.timedelta(minutes=30)


class TestAfkDue:
    def test_idle_player_gets_checked_once_the_time_is_up(self):
        confirmed = {"a": _T0, "b": _T0 + dt.timedelta(minutes=10)}
        to_check, to_remove = afk_due(confirmed, {}, _T0 + _AFTER, _AFTER)
        assert to_check == ["a"] and to_remove == []

    def test_no_second_check_while_one_is_pending(self):
        deadlines = {"a": _T0 + _AFTER + dt.timedelta(minutes=5)}
        to_check, to_remove = afk_due({"a": _T0}, deadlines, _T0 + _AFTER + dt.timedelta(minutes=1), _AFTER)
        assert to_check == [] and to_remove == []

    def test_unanswered_check_removes_at_the_deadline(self):
        deadline = _T0 + _AFTER + dt.timedelta(minutes=5)
        _, to_remove = afk_due({"a": _T0}, {"a": deadline}, deadline, _AFTER)
        assert to_remove == ["a"]


class _AfkCog:
    def __init__(self, queue):
        self.queue = queue
        self.activated = []
        self.deleted = []

    async def mark_active(self, uid):
        self.activated.append(uid)

    async def delete_quietly(self, message):
        self.deleted.append(message)


class TestAfkCheckButton:
    def test_confirming_restarts_the_clock(self):
        cog = _AfkCog({"1": object()})
        interaction = _Interaction(user_id=1)
        asyncio.run(AfkCheckView(cog, "1").confirm.callback(interaction))
        assert cog.activated == ["1"]
        assert "still in the queue" in interaction.response.message

    def test_only_the_checked_player_may_answer(self):
        cog = _AfkCog({"1": object()})
        interaction = _Interaction(user_id=2)
        asyncio.run(AfkCheckView(cog, "1").confirm.callback(interaction))
        assert cog.activated == [] and cog.deleted == []
        assert "isn't for you" in interaction.response.message

    def test_answering_after_leaving_just_clears_the_check(self):
        cog = _AfkCog({})
        interaction = _Interaction(user_id=1)
        asyncio.run(AfkCheckView(cog, "1").confirm.callback(interaction))
        assert cog.activated == [] and cog.deleted == [interaction.message]
        assert "no longer in the queue" in interaction.response.message


class TestNextRoster:
    def test_sit_outs_hand_their_spots_to_the_waitlist_in_order(self):
        roster, promoted, short = next_roster(["a", "b", "c", "d"], ["b"], ["x", "y"])
        assert roster == ["a", "c", "d", "x"] and promoted == ["x"] and short == 0

    def test_nobody_sitting_out_means_nobody_moves(self):
        assert next_roster(["a", "b"], [], ["x"]) == (["a", "b"], [], 0)

    def test_short_when_the_waitlist_cannot_cover_the_sit_outs(self):
        _, _, short = next_roster(["a", "b", "c", "d"], ["a", "b"], ["x"])
        assert short == 1


class TestSitOutAndWaitlist:
    """Between games, players give up spots with Sit out and others claim them
    with Waitlist; the swap happens at the next New teams."""

    def test_sit_out_then_new_teams_swaps_in_the_first_waitlister(self):
        view, users = _view(_Store(count=5))
        asyncio.run(view.waitlist.callback(_Interaction(user_id="101")))
        asyncio.run(view.waitlist.callback(_Interaction(user_id="102")))
        asyncio.run(view.sit_out.callback(_Interaction(user_id="3")))
        # No game stored yet, but the roster changed, so it re-balances.
        asyncio.run(view.new_teams.callback(_Interaction()))
        assert [u.id for u in view.cog.reposted] == [u.id for u in users if u.id != "3"] + ["101"]
        assert [u.id for u in view.cog.promoted] == ["101"]

    def test_pressing_again_takes_it_back(self):
        view, _ = _view(_Store(count=5))
        asyncio.run(view.sit_out.callback(_Interaction(user_id="3")))
        asyncio.run(view.sit_out.callback(_Interaction(user_id="3")))
        asyncio.run(view.waitlist.callback(_Interaction(user_id="101")))
        asyncio.run(view.waitlist.callback(_Interaction(user_id="101")))
        assert view.cog.sitting_out == [] and view.cog.waitlist == []

    def test_no_swap_without_someone_to_take_the_spot(self):
        view, _ = _view(_Store(count=5))
        asyncio.run(view.sit_out.callback(_Interaction(user_id="3")))
        interaction = _Interaction()
        asyncio.run(view.new_teams.callback(interaction))
        assert "need 1 more" in interaction.response.message
        assert view.cog.reposted is None
        assert view.cog.sitting_out == ["3"]  # still waiting for a taker

    def test_players_sit_out_and_outsiders_wait(self):
        view, _ = _view(_Store(count=5))
        outsider = _Interaction(user_id="stranger")
        asyncio.run(view.sit_out.callback(outsider))
        assert "Waitlist" in outsider.response.message
        player = _Interaction(user_id="3")
        asyncio.run(view.waitlist.callback(player))
        assert "Sit out" in player.response.message
        assert view.cog.sitting_out == [] and view.cog.waitlist == []


class TestNextGameButtonsOnSummary:
    """The same Sit out / Waitlist / New teams on the summary of the game just
    played, acting on the live roster."""

    def _view(self):
        users = [types.SimpleNamespace(id=str(i)) for i in range(8)]
        cog = _Cog(_Store(count=5), stored_ids=[u.id for u in users])
        cog.last_roster = users
        return NextGameView(cog), users

    def test_sit_out_and_new_teams_swap_in_the_waitlister(self):
        view, users = self._view()
        asyncio.run(view.waitlist.callback(_Interaction(user_id="101")))
        asyncio.run(view.sit_out.callback(_Interaction(user_id="3")))
        interaction = _Interaction()
        asyncio.run(view.new_teams.callback(interaction))
        assert [u.id for u in view.cog.promoted] == ["101"]
        assert not interaction.message.deleted  # the summary stays

    def test_only_players_on_the_roster_may_re_team(self):
        view, _ = self._view()
        interaction = _Interaction(user_id="stranger")
        asyncio.run(view.new_teams.callback(interaction))
        assert "Only a player" in interaction.response.message
        assert view.cog.reposted is None
