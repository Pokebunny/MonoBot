import asyncio
import datetime
from types import SimpleNamespace

from cogs.leaderboard import Leaderboard
from conftest import BASE, community_cog, make_match, make_player
from services.rating import season_book
from services.storage import hash_replay


class _Ctx:
    guild = None
    author = SimpleNamespace(id=1, display_name="Me")

    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(SimpleNamespace(content=content, **kwargs))
        return SimpleNamespace()


def _play(store, team1, team2, index, winning_team):
    players = [make_player(n, t, handle=f"h-{n}") for t, names in ((1, team1), (2, team2)) for n in names]
    match = make_match(
        players,
        winning_team=winning_team,
        file_name=f"g{index}.SC2Replay",
        played_at=BASE + datetime.timedelta(minutes=20 * index),
    )
    store.ingest(match, hash_replay(f"g{index}".encode()))


def _board(store, query):
    cog = community_cog(Leaderboard, store, get_user=lambda _id: None)
    ctx = _Ctx()
    asyncio.run(cog.leaderboard.callback(cog, ctx, query=query))
    return ctx.sent[-1].embed


def test_mmr_board_ranks_by_raw_rating_not_the_hedged_one(store):
    regulars, rivals = ["A0", "A1", "A2", "A3"], ["B0", "B1", "B2", "B3"]
    for i in range(8):
        _play(store, regulars, rivals, i, winning_team=1 if i % 2 else 2)
    # One game, one win: a high estimate held with little confidence.
    _play(store, ["New", "A1", "A2", "A3"], rivals, 8, winning_team=1)

    book = season_book(store, store.current_season())
    by_mu = [r.name for r in sorted(book.leaderboard(), key=lambda r: r.mu, reverse=True)]
    by_ordinal = [r.name for r in book.leaderboard()]
    assert by_mu != by_ordinal  # otherwise the test proves nothing

    embed = _board(store, "mmr 1")
    assert "Raw MMR" in embed.title
    shown = [line.split("**")[1] for line in embed.description.splitlines()]
    assert shown == by_mu
    top = book.rating_for(f"h-{by_mu[0]}")
    assert f"**{top.raw_rating}**" in embed.description.splitlines()[0]

    assert [line.split("**")[1] for line in _board(store, "1").description.splitlines()] == by_ordinal
