"""Slash invocations are deferred before the command runs, with the visibility
the reply will get: the first message after a defer takes the defer's."""

import asyncio
from types import SimpleNamespace

from context import MonoContext


def _ctx(command_name, interaction=True, responded=False):
    ctx = MonoContext.__new__(MonoContext)
    ctx.command = SimpleNamespace(name=command_name)
    ctx.interaction = SimpleNamespace(response=SimpleNamespace(is_done=lambda: responded)) if interaction else None
    ctx.deferred = []

    async def defer(*, ephemeral=False):
        ctx.deferred.append(ephemeral)

    ctx.defer = defer
    return ctx


def test_slash_command_defers_privately():
    ctx = _ctx("profile")
    asyncio.run(ctx.defer_slash())
    assert ctx.deferred == [True]


def test_always_public_command_defers_publicly():
    ctx = _ctx("queue")
    asyncio.run(ctx.defer_slash())
    assert ctx.deferred == [False]


def test_text_command_is_not_deferred():
    ctx = _ctx("profile", interaction=False)
    asyncio.run(ctx.defer_slash())
    assert ctx.deferred == []


def test_already_answered_interaction_is_left_alone():
    ctx = _ctx("profile", responded=True)
    asyncio.run(ctx.defer_slash())
    assert ctx.deferred == []
