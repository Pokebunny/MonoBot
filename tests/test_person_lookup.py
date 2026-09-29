"""Every command that takes a player resolves them through views.person_or_pick:
SC2 names first, then Discord members, and a picker whenever the name is
shared. These drive the commands built on it."""

import asyncio
import datetime
from types import SimpleNamespace

import pytest
from discord.ext import commands
from models.replay import MatchPlayer, MonobattleMatch
from services import identity
from services.storage import MatchStore, hash_replay
from views import person_or_pick

BASE = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


def _play(store, roster, days):
    """roster: (display name, handle) for team 1 then team 2."""
    at = BASE + datetime.timedelta(days=days)
    players = [
        MatchPlayer(
            name=name,
            toon_handle=handle,
            team=1 if i < len(roster) // 2 else 2,
            race="Zerg",
            pick="Zergling",
            unit_counts={"Zergling": 10},
        )
        for i, (name, handle) in enumerate(roster)
    ]
    match = MonobattleMatch(
        file_name=f"{at.isoformat()}.SC2Replay",
        map_name="Monobattle LotV - Map Rotation",
        played_at=at,
        duration_seconds=900,
        game_type="3v3",
        pick_mode="blind_random",
        pick_phase_seconds=60,
        players=players,
        winning_team=1,
        winner_confidence=1.0,
        winner_method="recorded",
    )
    store.ingest(match, hash_replay(str(at).encode()))


def _filler(n, tag="f"):
    return [(f"{tag.upper()}{i}", f"h-{tag}{i}") for i in range(n)]


def _member(uid, name, display_name=None, nick=None):
    return SimpleNamespace(id=uid, name=name, global_name=None, nick=nick, display_name=display_name or name)


class _Guild:
    def __init__(self, *members):
        self.members = list(members)

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)


class _Ctx:
    def __init__(self, *members):
        self.guild = _Guild(*members)
        self.author = SimpleNamespace(id=999, display_name="Me")
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(SimpleNamespace(content=content, **kwargs))
        return SimpleNamespace()


@pytest.fixture(autouse=True)
def no_stock_member_lookup(monkeypatch):
    """The stock converter needs a live bot; the case-insensitive fallback
    over ctx.guild.members is what these tests exercise."""

    async def stock(self, ctx, argument):
        raise commands.MemberNotFound(argument)

    monkeypatch.setattr(commands.MemberConverter, "convert", stock)


@pytest.fixture
def store(tmp_path):
    s = MatchStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _run(coro):
    return asyncio.run(coro)


# -- the shared lookup ---------------------------------------------------


def test_an_sc2_name_beats_a_discord_member_called_the_same(store):
    _play(store, [("Rudy", "h-rudy"), *_filler(5)], 0)
    ctx = _Ctx(_member(5, "rudy"))
    people = _run(person_or_pick(ctx, store, "Rudy", None))
    assert people[0].handles == ("h-rudy",)
    assert people[0].via != identity.DISCORD


def test_a_discord_member_is_found_when_no_account_matches(store):
    ctx = _Ctx(_member(5, "mfbirdman", display_name="MFBirdman"))
    people = _run(person_or_pick(ctx, store, "MFBirdman", None))
    assert (people[0].discord_id, people[0].via) == ("5", identity.DISCORD)


def test_nothing_matching_is_answered_for_the_caller(store):
    ctx = _Ctx()
    assert _run(person_or_pick(ctx, store, "ghost", None)) is None
    assert "No player found" in ctx.sent[0].content


def test_a_shared_sc2_name_gets_the_picker(store):
    _play(store, [("Twin", "h-one"), *_filler(5)], 0)
    _play(store, [("Twin", "h-two"), *_filler(5)], 1)
    ctx = _Ctx()
    assert _run(person_or_pick(ctx, store, "Twin", None)) is None
    assert "which one?" in ctx.sent[0].content and ctx.sent[0].view is not None


# -- !unlinkuser -----------------------------------------------------------


@pytest.fixture
def jay(store):
    """One person, two accounts: Jay and Luigi, both linked to Discord 7."""
    _play(store, [("Jay", "h-jay"), *_filler(5)], 0)
    _play(store, [("Luigi", "h-luigi"), *_filler(5)], 1)
    store.link_player("7", "Jay")
    store.add_account("7", "h-luigi")
    return store


def _identity_cog(store):
    from cogs.identity import Identity

    return Identity(SimpleNamespace(match_store=store))


def test_unlinking_an_sc2_name_drops_only_that_name(jay):
    cog = _identity_cog(jay)
    ctx = _Ctx(_member(7, "jaybird"))
    _run(cog.unlinkuser.callback(cog, ctx, "Luigi"))
    assert jay.handles_for("7") == ["h-jay"]


def test_unlinking_a_discord_member_drops_every_account(jay):
    cog = _identity_cog(jay)
    ctx = _Ctx(_member(7, "jaybird"))
    _run(cog.unlinkuser.callback(cog, ctx, "JAYBIRD"))
    assert jay.handles_for("7") == []


def test_unlinking_an_unlinked_account_says_so(store):
    _play(store, [("Loner", "h-loner"), *_filler(5)], 0)
    cog = _identity_cog(store)
    ctx = _Ctx()
    _run(cog.unlinkuser.callback(cog, ctx, "Loner"))
    assert "isn't linked" in ctx.sent[0].content


# -- !whois ----------------------------------------------------------------


def test_whois_by_sc2_name_shows_the_linked_member(jay):
    cog = _identity_cog(jay)
    ctx = _Ctx(_member(7, "jaybird", display_name="Jaybird"))
    _run(cog.whois.callback(cog, ctx, target="Luigi"))
    embed = ctx.sent[0].embed
    assert embed.title == "Jaybird"
    assert "Jay" in embed.description and "Luigi" in embed.description


def test_whois_on_an_unlinked_account_lists_it(store):
    _play(store, [("Loner", "h-loner"), *_filler(5)], 0)
    cog = _identity_cog(store)
    ctx = _Ctx()
    _run(cog.whois.callback(cog, ctx, target="loner"))
    assert "Not linked" in ctx.sent[0].embed.description


# -- !h2h ------------------------------------------------------------------


def _leaderboard_cog(store):
    from cogs.leaderboard import Leaderboard

    return Leaderboard(SimpleNamespace(match_store=store, get_user=lambda _id: None))


def test_h2h_accepts_discord_names(store):
    _play(
        store, [("Ann", "h-ann"), ("X1", "h-x1"), ("X2", "h-x2"), ("Bob", "h-bob"), ("Y1", "h-y1"), ("Y2", "h-y2")], 0
    )
    store.link_player("1", "Ann")
    store.link_player("2", "Bob")
    cog = _leaderboard_cog(store)
    ctx = _Ctx(_member(1, "annie", display_name="Annie"), _member(2, "bobby", display_name="Bobby"))
    _run(cog.h2h.callback(cog, ctx, "ANNIE", "bobby"))
    assert ctx.sent[-1].embed is not None


def test_h2h_asks_about_a_shared_first_name_before_the_second(store):
    _play(store, [("Twin", "h-one"), *_filler(5)], 0)
    _play(store, [("Twin", "h-two"), *_filler(5)], 1)
    cog = _leaderboard_cog(store)
    ctx = _Ctx()
    _run(cog.h2h.callback(cog, ctx, "Twin", "ghost"))
    assert len(ctx.sent) == 1 and "which one?" in ctx.sent[0].content
