import asyncio
import datetime as dt
import types

import discord
import pytest
from cogs.matchmaking import (
    ACTIVE_GAME_MINUTES,
    MATCH_POSTED_AT_META_KEY,
    MATCH_ROSTER_META_KEY,
    QUEUE_MSG_META_KEY,
    EndSessionView,
    LegacyLineupView,
    Lobby,
    SkillLevelView,
)
from models.config import GuildConfig
from models.matchmaking import QueuedPlayer
from services import match_embeds
from services.matchmaking import balance_teams, expired, next_roster, ranked_matches
from services.rating import DEFAULT_MU, DEFAULT_SIGMA, SKILL_LEVEL_MU, predict_win_probability


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


def test_team1_holds_the_strongest_player():
    # Join order no longer decides the sides: the weakest player joined first,
    # and every option still puts the strongest on Team 1, with the win chance
    # still read from Team 1's side.
    players = [_p("w", mu=15, sigma=2)] + [_p(f"p{i}", mu=20 + 3 * i, sigma=2) for i in range(7)]
    for option in ranked_matches(players):
        assert option.team1[0].display_name == "p6"
        assert predict_win_probability(
            [(p.mu, p.sigma) for p in option.team1], [(p.mu, p.sigma) for p in option.team2]
        ) == pytest.approx(option.team1_win_probability)


def test_teams_list_strongest_first_by_mu():
    # By mu, not the board's mu - 3*sigma: an uncertain newcomer the balancer
    # rates highly still tops their team.
    # Here "new" has the top mu but the board would rank them last.
    players = [_p("new", mu=40, sigma=8), *(_p(f"p{i}", mu=20 + 2 * i, sigma=2) for i in range(7))]
    for option in ranked_matches(players):
        assert option.team1[0].display_name == "new"
        for team in (option.team1, option.team2):
            mus = [p.mu for p in team]
            assert mus == sorted(mus, reverse=True)


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


_T0 = dt.datetime(2026, 9, 27, 20, 0, tzinfo=dt.UTC)
_TIMEOUT = dt.timedelta(minutes=60)


class TestQueueTimeoutRule:
    def test_removed_once_the_timeout_has_passed(self):
        joined = {"a": _T0, "b": _T0 + dt.timedelta(minutes=10)}
        assert expired(joined, _T0 + _TIMEOUT, _TIMEOUT) == ["a"]


class TestNextRoster:
    def test_sit_outs_hand_their_spots_to_the_line_in_order(self):
        roster, promoted, short = next_roster(["a", "b", "c", "d"], ["b"], ["x", "y"])
        assert roster == ["a", "c", "d", "x"] and promoted == ["x"] and short == 0

    def test_nobody_sitting_out_means_nobody_moves(self):
        assert next_roster(["a", "b"], [], ["x"]) == (["a", "b"], [], 0)

    def test_short_when_the_line_cannot_cover_the_sit_outs(self):
        _, _, short = next_roster(["a", "b", "c", "d"], ["a", "b"], ["x"])
        assert short == 1


# --- fakes -------------------------------------------------------------------


class _Store:
    def __init__(self):
        self.meta = {}
        self.count = 5
        self.handles = {}  # uid -> toon handles, for anyone who has played
        self.unlinked = set()
        self.level = None

    def get_meta(self, key):
        return self.meta.get(key)

    def set_meta(self, key, value):
        self.meta[key] = value

    def match_count(self):
        return self.count

    def sc2_names_for(self, uid):
        return [] if uid in self.unlinked else ["linked"]

    def handles_for(self, uid):
        return list(self.handles.get(uid, ()))

    def skill_level(self, uid):
        return self.level

    def set_skill_level(self, uid, level):
        self.level = level


def _user(uid):
    return types.SimpleNamespace(id=int(uid), display_name=f"p{uid}")


class _Guild:
    """Everyone is still in the server."""

    def get_member(self, uid):
        return _user(uid)


class _Message:
    def __init__(self, id, channel=None, **kwargs):
        self.id = id
        self.channel = channel or types.SimpleNamespace(id=9)
        self.kwargs = kwargs
        self.content = kwargs.get("content")
        self.embeds = [kwargs["embed"]] if kwargs.get("embed") is not None else []
        self.deleted = False
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if kwargs.get("embed") is not None:
            self.embeds = [kwargs["embed"]]

    async def delete(self):
        self.deleted = True


class _Channel:
    id = 9

    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        message = _Message(100 + len(self.sent), channel=self, content=content, **kwargs)
        self.sent.append(message)
        return message


class _Response:
    def __init__(self):
        self.edited = self.deferred = False
        self.message = None

    async def edit_message(self, **kwargs):
        self.edited = True

    async def defer(self):
        self.deferred = True

    async def send_message(self, content, view=None, ephemeral=False):
        self.message = content
        self.view = view


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content, view=None, ephemeral=False):
        self.sent.append((content, view, ephemeral))


class _Interaction:
    def __init__(self, uid, message=None, channel=None):
        self.user = _user(uid)
        self.response = _Response()
        self.followup = _Followup()
        self.guild = _Guild()
        self.channel = channel or _Channel()
        self.message = message or _Message(1)


def _lobby(store=None):
    store = store or _Store()
    community = types.SimpleNamespace(
        config=GuildConfig(), store=store, ratings=types.SimpleNamespace(book=lambda: None), guild_id=1
    )
    holder = {}
    lobby = Lobby(None, community, lambda _guild: holder["lobby"])
    holder["lobby"] = lobby
    return lobby


def _game_on(lobby, uids=range(8), minutes_ago=10):
    """A group of `uids` has teams up, posted `minutes_ago`."""
    users = [_user(u) for u in uids]
    lobby.last_roster = users
    lobby.store.set_meta(MATCH_ROSTER_META_KEY, ",".join(str(u.id) for u in users))
    posted = discord.utils.utcnow() - dt.timedelta(minutes=minutes_ago)
    lobby.store.set_meta(MATCH_POSTED_AT_META_KEY, posted.isoformat())
    return users


def run(coro):
    return asyncio.run(coro)


# --- the queue (no game on) ----------------------------------------------------


class TestQueue:
    def test_join_on_the_queue_message_redraws_it_in_place(self):
        lobby = _lobby()
        lobby.message = _Message(1)
        interaction = _Interaction(1, message=lobby.message)
        run(lobby.handle_join(interaction))
        assert lobby.line == ["1"] and "1" in lobby.joined_at
        assert interaction.response.edited and interaction.response.message is None

    def test_join_elsewhere_replies_privately_and_redraws_the_queue(self):
        lobby = _lobby()
        lobby.message = _Message(1)
        interaction = _Interaction(1, message=_Message(2))
        run(lobby.handle_join(interaction))
        assert interaction.response.message == "You're in the queue."
        assert lobby.message.edits  # the queue message shows them

    def test_unlinked_players_are_told_to_link(self):
        store = _Store()
        store.unlinked.add("1")
        lobby = _lobby(store)
        interaction = _Interaction(1)
        run(lobby.handle_join(interaction))
        assert "!link" in interaction.response.message and lobby.line == []

    def test_joining_again_restarts_the_timer(self):
        lobby = _lobby()
        lobby.join(_user(1), _Guild())
        lobby.joined_at["1"] = _T0
        outcome = lobby.join(_user(1), _Guild())
        assert "restarted" in outcome.reply and lobby.joined_at["1"] > _T0

    def test_leave_takes_them_out(self):
        lobby = _lobby()
        lobby.join(_user(1), _Guild())
        outcome = lobby.leave("1")
        assert lobby.line == [] and outcome.quiet

    def test_the_eighth_join_hands_over_the_first_eight(self):
        lobby = _lobby()
        for uid in range(1, 8):
            lobby.join(_user(uid), _Guild())
        outcome = lobby.join(_user(8), _Guild())
        assert [u.id for u in outcome.roster] == list(range(1, 9)) and lobby.line == []

    def test_the_queue_lists_mentions_and_flags_the_unrated(self):
        store = _Store()
        store.handles["1"] = ["h1"]
        lobby = _lobby(store)
        lobby.join(_user(1), _Guild())
        lobby.join(_user(2), _Guild())
        assert lobby._embed().description.startswith("• <@1>\n• <@2> *(unrated)*")

    def test_a_restart_keeps_the_line(self):
        lobby = _lobby()
        lobby.join(_user(1), _Guild())
        again = _lobby(lobby.store)
        assert again.line == ["1"] and "1" in again.joined_at


class TestFullQueue:
    """The 8th join: answered before the slow work, and a failed post never
    loses the roster."""

    def _eighth_join(self, monkeypatch, post_match):
        import cogs.matchmaking as mm

        monkeypatch.setattr(mm, "POST_RETRY_DELAYS", (0, 0))
        lobby = _lobby()
        for uid in range(2, 9):
            lobby.join(_user(uid), _Guild())
        lobby.post_match = post_match
        sent = []

        async def send(content):
            sent.append(content)

        interaction = _Interaction(1, channel=types.SimpleNamespace(send=send))
        run(lobby.handle_join(interaction))
        return lobby, interaction, sent

    def test_the_press_is_answered_and_the_match_posted(self, monkeypatch):
        posted = []

        async def post_match(channel, users, announce=False):
            posted.append(len(users))

        lobby, interaction, sent = self._eighth_join(monkeypatch, post_match)
        assert interaction.response.message and posted == [8] and sent == []
        assert lobby.line == [] and len(lobby.last_roster) == 8

    def test_a_failed_post_is_retried_then_points_to_teams(self, monkeypatch):
        calls = []

        async def post_match(channel, users, announce=False):
            calls.append(1)
            raise RuntimeError("Discord 503")

        lobby, _, sent = self._eighth_join(monkeypatch, post_match)
        assert len(calls) == 3
        assert len(sent) == 1 and "!teams" in sent[0]
        assert len(lobby.last_roster) == 8  # so !teams has the roster to split
        assert lobby.store.meta[MATCH_ROSTER_META_KEY].count(",") == 7


class TestPostedTeams:
    def test_teams_become_the_lobby_message_and_the_queue_goes(self):
        lobby = _lobby()
        channel = _Channel()
        old = _Message(1, channel=channel)
        lobby.message = old
        lobby.store.set_meta(QUEUE_MSG_META_KEY, "9:1")
        users = [_user(u) for u in range(8)]
        run(lobby.post_match(channel, users, announce=True))
        teams = channel.sent[0]
        assert lobby.message is teams and old.deleted
        assert "your match is ready" in teams.content
        labels = [item.label for item in teams.kwargs["view"].children]
        assert labels == ["Join", "Leave", "New teams", "End session"]

    def test_the_queue_message_has_no_new_teams_button(self):
        labels = [item.label for item in _lobby()._view().children]
        assert labels == ["Join", "Leave"]


# --- while a game is on ----------------------------------------------------------


class TestDuringAGame:
    def test_an_outsider_joins_the_line(self):
        lobby = _lobby()
        _game_on(lobby)
        interaction = _Interaction(101)
        run(lobby.handle_join(interaction))
        assert lobby.line == ["101"] and "#1 in line" in interaction.response.message

    def test_eight_in_line_do_not_start_a_second_match(self):
        lobby = _lobby()
        _game_on(lobby)
        outcomes = [lobby.join(_user(100 + i), _Guild()) for i in range(9)]
        assert all(o.roster is None for o in outcomes) and len(lobby.line) == 9

    def test_a_player_leaving_sits_out_and_join_brings_them_back(self):
        lobby = _lobby()
        _game_on(lobby)
        assert "sit out" in lobby.leave("3").reply and lobby.sitting_out == ["3"]
        assert "already sitting out" in lobby.leave("3").reply
        assert "back in" in lobby.join(_user(3), _Guild()).reply and lobby.sitting_out == []

    def test_a_player_joining_is_told_they_are_playing(self):
        lobby = _lobby()
        _game_on(lobby)
        assert "already in this game" in lobby.join(_user(3), _Guild()).reply

    def test_the_teams_show_who_is_sitting_out_and_who_is_in_line(self):
        lobby = _lobby()
        _game_on(lobby)
        lobby.leave("3")
        lobby.join(_user(101), _Guild())
        fields = {f.name: f.value for f in lobby._embed().fields}
        assert fields["Sitting out next game"] == "<@3>"
        assert fields["In line for a spot"] == "1. <@101>"

    def test_waiting_in_line_does_not_time_out_during_a_game(self):
        lobby = _lobby()
        _game_on(lobby)
        lobby.join(_user(101), _Guild())
        lobby.joined_at["101"] = _T0  # long ago
        run(lobby.sweep_timeouts())
        assert lobby.line == ["101"]

    def test_the_line_clock_starts_when_the_session_ends(self):
        lobby = _lobby()
        # Teams went up 100 minutes ago: the session ended 10 minutes ago.
        _game_on(lobby, minutes_ago=ACTIVE_GAME_MINUTES + 10)
        lobby.line = ["101"]
        lobby.joined_at["101"] = discord.utils.utcnow() - dt.timedelta(minutes=95)
        run(lobby.sweep_timeouts())
        assert lobby.line == ["101"]  # 10 minutes into the queue, not 95

    def test_once_the_session_ends_the_line_is_the_queue(self):
        lobby = _lobby()
        _game_on(lobby, minutes_ago=ACTIVE_GAME_MINUTES + 1)
        lobby.line = [str(100 + i) for i in range(7)]
        outcome = lobby.join(_user(200), _Guild())
        assert outcome.roster is not None and len(outcome.roster) == 8

    def test_commands_speak_of_the_person_by_name(self):
        lobby = _lobby()
        _game_on(lobby)
        assert lobby.join(_user(101), _Guild(), "Zed").reply.startswith("**Zed** is #1 in line")
        assert lobby.leave("3", "Ann").reply.startswith("**Ann** will sit out")


class TestNewTeams:
    def _posted(self):
        lobby = _lobby()
        users = _game_on(lobby)
        channel = _Channel()
        run(lobby.post_match(channel, users))
        return lobby, channel

    def test_cycles_the_splits_in_place_until_a_game_is_played(self):
        lobby, channel = self._posted()
        interaction = _Interaction(1, message=lobby.message, channel=channel)
        run(lobby.handle_new_teams(interaction))
        assert lobby.option_index == 1 and interaction.response.edited and len(channel.sent) == 1

    def test_rebalances_once_a_game_is_stored(self):
        lobby, channel = self._posted()
        lobby.store.count += 1
        run(lobby.handle_new_teams(_Interaction(1, message=lobby.message, channel=channel)))
        assert len(channel.sent) == 2 and lobby.message is channel.sent[1] and channel.sent[0].deleted

    def test_a_sit_out_swaps_in_the_first_in_line_and_pings_them(self):
        lobby, channel = self._posted()
        lobby.join(_user(101), _Guild())
        lobby.join(_user(102), _Guild())
        lobby.leave("3")
        run(lobby.handle_new_teams(_Interaction(1, channel=channel)))
        roster = lobby.stored_roster_ids()
        assert "3" not in roster and "101" in roster and lobby.line == ["102"]
        assert channel.sent[-1].content.startswith("<@101>") and lobby.sitting_out == []

    def test_no_swap_without_someone_to_take_the_spot(self):
        lobby, channel = self._posted()
        lobby.leave("3")
        interaction = _Interaction(1, channel=channel)
        run(lobby.handle_new_teams(interaction))
        assert "need 1 more" in interaction.response.message and len(channel.sent) == 1

    def test_only_players_in_the_match_may_re_team(self):
        lobby, channel = self._posted()
        interaction = _Interaction(999, channel=channel)
        run(lobby.handle_new_teams(interaction))
        assert "Only a player" in interaction.response.message

    def test_after_a_restart_rebalances_the_stored_roster(self):
        lobby = _lobby()
        _game_on(lobby)
        lobby.last_roster = []  # a restart lost the users and the splits
        channel = _Channel()
        run(lobby.handle_new_teams(_Interaction(1, channel=channel)))
        assert len(channel.sent) == 1 and lobby.options

    def test_teams_command_re_splits(self):
        lobby, channel = self._posted()
        ctx = types.SimpleNamespace(guild=_Guild(), channel=channel, send=channel.send)
        run(lobby.teams(ctx))
        assert len(channel.sent) == 2


class TestEndSession:
    def _posted(self):
        lobby = _lobby()
        users = _game_on(lobby)
        channel = _Channel()
        run(lobby.post_match(channel, users))
        return lobby, channel

    def test_asks_first_and_only_players_in_the_match_may(self):
        lobby, _ = self._posted()
        stranger = _Interaction(999)
        run(lobby.handle_end_session(stranger))
        assert "Only a player" in stranger.response.message
        player = _Interaction(1)
        run(lobby.handle_end_session(player))
        assert isinstance(player.response.view, EndSessionView) and lobby.game_active()

    def test_the_line_becomes_the_queue_with_fresh_clocks(self):
        lobby, channel = self._posted()
        lobby.join(_user(101), _Guild())
        lobby.join(_user(102), _Guild())
        lobby.leave("3")
        lobby.joined_at["101"] -= dt.timedelta(hours=3)
        run(lobby.end_session(channel, _Guild(), "p1"))
        assert not lobby.game_active() and lobby.line == ["101", "102"] and lobby.sitting_out == []
        assert discord.utils.utcnow() - lobby.joined_at["101"] < dt.timedelta(minutes=1)
        assert lobby.message is channel.sent[-1] and channel.sent[0].deleted
        assert channel.sent[-1].content == "p1 ended the session — the queue's open."
        assert "End session" not in [b.label for b in channel.sent[-1].kwargs["view"].children]

    def test_a_full_line_forms_the_next_match(self):
        lobby, channel = self._posted()
        for uid in range(101, 109):
            lobby.join(_user(uid), _Guild())
        run(lobby.end_session(channel, _Guild(), "p1"))
        assert lobby.line == [] and set(lobby.stored_roster_ids()) == {str(u) for u in range(101, 109)}

    def test_a_session_that_runs_out_redraws_the_lobby(self):
        lobby, _ = self._posted()
        lobby.message = _Message(1)
        run(lobby.notice_session_end())
        assert lobby.message.edits == []
        lobby.store.set_meta(
            MATCH_POSTED_AT_META_KEY,
            (discord.utils.utcnow() - dt.timedelta(minutes=ACTIVE_GAME_MINUTES + 1)).isoformat(),
        )
        run(lobby.notice_session_end())
        run(lobby.notice_session_end())
        assert len(lobby.message.edits) == 1


class TestOldButtons:
    """Proposals and summaries posted before the redesign carry Sit out /
    Waitlist; they act as Leave / Join."""

    def test_waitlist_joins_and_sit_out_leaves(self):
        lobby = _lobby()
        _game_on(lobby)
        view = LegacyLineupView(lobby.lobby_for)
        run(view.match_waitlist.callback(_Interaction(101)))
        run(view.next_sit_out.callback(_Interaction(3)))
        assert lobby.line == ["101"] and lobby.sitting_out == ["3"]

    def test_an_old_waitlist_field_is_replaced_not_duplicated(self):
        embed = discord.Embed(title="t")
        embed.add_field(name="Waitlist", value="1. <@5>")
        fields = [f.name for f in match_embeds.with_lineup_changes(embed, [], ["7"]).fields]
        assert fields == ["In line for a spot"]


class TestRestart:
    def test_the_lobby_message_is_picked_back_up(self):
        lobby = _lobby()
        lobby.join(_user(1), _Guild())
        lobby.store.set_meta(QUEUE_MSG_META_KEY, "9:42")
        message = _Message(42)

        async def fetch(ref):
            return message

        lobby._fetch_message_ref = fetch
        run(lobby.restore_message())
        assert lobby.message is message and message.edits

    def test_a_gone_message_clears_the_pointer(self):
        lobby = _lobby()
        lobby.store.set_meta(QUEUE_MSG_META_KEY, "9:42")

        async def fetch(ref):
            return None

        lobby._fetch_message_ref = fetch
        run(lobby.restore_message())
        assert lobby.message is None and lobby.store.meta[QUEUE_MSG_META_KEY] == ""


class TestMeetingPoint:
    def test_teams_name_the_channel_and_region(self):
        lobby = _lobby()
        lobby.config = GuildConfig(sc2_chat_channel="bgh1")
        run(lobby.post_match(_Channel(), _game_on(lobby)))
        assert "**bgh1** (Americas server)" in lobby._embed().description

    def test_queue_names_the_channel(self):
        meet = match_embeds.meeting_point("mono", "Americas")
        assert meet in match_embeds.queue_status([], 8, meet=meet).description


class TestSkillLevel:
    """A player who hasn't played here is asked their SC2 level on Join."""

    def test_first_join_asks_privately(self):
        lobby = _lobby()
        interaction = _Interaction(1)
        run(lobby.handle_join(interaction))
        [(_, view, ephemeral)] = interaction.followup.sent
        assert ephemeral and isinstance(view, SkillLevelView)

    def test_not_asked_once_set(self):
        store = _Store()
        store.level = "mid"
        interaction = _Interaction(1)
        run(_lobby(store).handle_join(interaction))
        assert interaction.followup.sent == []

    def test_not_asked_once_played(self):
        store = _Store()
        store.handles["1"] = ["1-S2-1-1"]
        interaction = _Interaction(1)
        run(_lobby(store).handle_join(interaction))
        assert interaction.followup.sent == []

    def test_locked_after_first_game(self):
        store = _Store()
        store.handles["1"] = ["1-S2-1-1"]
        assert "comes from your games" in _lobby(store).set_level(_user(1), "top")
        assert store.level is None

    def test_unplayed_player_balances_at_their_level(self):
        store = _Store()
        store.level = "top"
        player = _lobby(store)._queued_player(_user(1))
        assert player.mu == SKILL_LEVEL_MU["top"] and not player.rated
