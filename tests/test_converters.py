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
