"""Typed member names match regardless of case, exact case first."""

import asyncio
from types import SimpleNamespace

import converters
import pytest
from discord.ext import commands


def _member(name, global_name=None, nick=None):
    return SimpleNamespace(name=name, global_name=global_name, nick=nick)


def _guild(*members):
    return SimpleNamespace(members=list(members))


named = converters.members_named_ignoring_case


def test_username_ignores_case():
    bob = _member("bob")
    assert named(_guild(bob, _member("alice")), "BOB") == [bob]


def test_nickname_and_display_name_ignore_case():
    rudy = _member("r_1234", global_name="ARudy")
    jay = _member("j_1", nick="JayBird")
    guild = _guild(rudy, jay)
    assert named(guild, "arudy") == [rudy]
    assert named(guild, "jaybird") == [jay]


def test_a_username_is_the_answer_on_its_own():
    # Usernames are unique, so someone whose nickname happens to be another
    # member's username doesn't make that username ambiguous.
    bob = _member("bob")
    assert named(_guild(bob, _member("x", nick="Bob")), "bob") == [bob]


def test_a_shared_display_name_returns_everyone_sharing_it():
    a, b = _member("a", nick="Twin"), _member("b", global_name="twin")
    assert named(_guild(a, b), "TWIN") == [a, b]


def test_no_guild_or_blank_name_matches_no_one():
    assert named(None, "bob") == []
    assert named(_guild(_member("bob")), "  ") == []


def test_the_stock_lookup_wins_before_the_fallback(monkeypatch):
    exact, other = _member("x", nick="Twin"), _member("y", nick="twin")

    async def stock(self, ctx, argument):
        if argument == "Twin":
            return exact
        raise commands.MemberNotFound(argument)

    monkeypatch.setattr(commands.MemberConverter, "convert", stock)
    ctx = SimpleNamespace(guild=_guild(exact, other))
    convert = converters.MemberConverter().convert
    assert asyncio.run(convert(ctx, "Twin")) is exact
    with pytest.raises(converters.AmbiguousMember) as err:
        asyncio.run(convert(ctx, "TWIN"))  # both match ignoring case: the caller asks which
    assert err.value.members == [exact, other]
    assert isinstance(err.value, commands.MemberNotFound)  # commands without a picker just report it


# -- stats commands fall back to Discord names ---------------------------


class _Ctx:
    def __init__(self, guild):
        self.guild = guild
        self.author = SimpleNamespace(id=999)
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)
        return SimpleNamespace()


def _no_stock_match(monkeypatch):
    async def stock(self, ctx, argument):
        raise commands.MemberNotFound(argument)

    monkeypatch.setattr(commands.MemberConverter, "convert", stock)


@pytest.fixture
def cog(tmp_path):
    from cogs.leaderboard import Leaderboard
    from services.storage import MatchStore

    store = MatchStore(str(tmp_path / "t.db"))
    yield Leaderboard(SimpleNamespace(match_store=store))
    store.close()


def test_a_name_no_account_matches_is_tried_as_a_discord_member(cog, monkeypatch):
    _no_stock_match(monkeypatch)
    cog.store.link_player("42", "Birdman")
    cog.store.handles_for = lambda discord_id: ["h-bird"] if discord_id == "42" else []
    ctx = _Ctx(_guild(SimpleNamespace(id=42, name="mfbirdman", global_name=None, nick=None, display_name="MFBirdman")))
    person, note = asyncio.run(cog._person_or_pick(ctx, "MFBIRDMAN", None))
    assert (person.discord_id, person.discord_name, person.handles) == ("42", "MFBirdman", ("h-bird",))


def test_a_discord_member_with_no_account_says_so(cog, monkeypatch):
    _no_stock_match(monkeypatch)
    ctx = _Ctx(_guild(SimpleNamespace(id=7, name="newbie", global_name=None, nick=None, display_name="Newbie")))
    assert asyncio.run(cog._person_or_pick(ctx, "newbie", None)) is None
    assert "hasn't linked" in ctx.sent[0]


def test_a_shared_discord_name_gets_the_picker(cog, monkeypatch):
    _no_stock_match(monkeypatch)
    a = SimpleNamespace(id=1, name="a", global_name="Twin", nick=None, display_name="Twin")
    b = SimpleNamespace(id=2, name="b", global_name=None, nick="twin", display_name="twin")
    ctx = _Ctx(_guild(a, b))
    assert asyncio.run(cog._person_or_pick(ctx, "TWIN", None)) is None
    assert "which one?" in ctx.sent[0]
