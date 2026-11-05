"""All-time records for !records: the most extreme games and single-game
performances in a community's history, one board per category.

Pure functions over stored matches. Game records count only decided games
(recap.is_decided — the gate every other stat uses), so an abandoned replay
can't hold a record. Durations are battle time, the clock every summary
shows.

No fastest-wins board: with a two-minute minimum on rated games, the top of
it was always games a team quit just past the cutoff (all ten on the
Monobattle server's board fell between 2:00 and 2:19)."""

import datetime
from typing import Callable, NamedTuple

from models.replay import MatchPlayer, MonobattleMatch
from services.awards import trade_efficiency
from services.recap import is_decided

RECORDS_SHOWN = 10


class RecordRow(NamedTuple):
    value: str  # the record itself, already formatted
    who: str
    match_id: int
    played_at: datetime.datetime


class RecordBoard(NamedTuple):
    key: str
    emoji: str
    title: str
    rows: list[RecordRow]


def _duration(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def _winners(match: MonobattleMatch) -> str:
    return ", ".join(p.name for p in match.team(match.winning_team))


def _game_board(
    key: str,
    emoji: str,
    title: str,
    matches: list[tuple[int, MonobattleMatch]],
    score: Callable[[MonobattleMatch], float | None],
    show: Callable[[MonobattleMatch], str],
) -> RecordBoard:
    scored = [(score(m), i, m) for i, m in matches if is_decided(m) and score(m) is not None]
    scored.sort(key=lambda t: -t[0])
    rows = [RecordRow(show(m), _winners(m), i, m.played_at) for _, i, m in scored[:RECORDS_SHOWN]]
    return RecordBoard(key, emoji, title, rows)


def _player_board(
    key: str,
    emoji: str,
    title: str,
    matches: list[tuple[int, MonobattleMatch]],
    score: Callable[[MatchPlayer], float | None],
    show: Callable[[float], str] = lambda value: f"{value:,}",
) -> RecordBoard:
    scored = [(score(p), i, m, p) for i, m in matches for p in m.players if score(p)]
    scored.sort(key=lambda t: -t[0])
    rows = [
        RecordRow(show(value), f"{p.name} ({p.pick or '?'})", i, m.played_at)
        for value, i, m, p in scored[:RECORDS_SHOWN]
    ]
    return RecordBoard(key, emoji, title, rows)


def record_boards(matches: list[tuple[int, MonobattleMatch]]) -> list[RecordBoard]:
    """Every category's top rows, in page order."""
    return [
        _game_board(
            "comebacks",
            "🔄",
            "Biggest Comebacks",
            matches,
            lambda m: m.comeback_deficit or None,
            lambda m: f"{m.comeback_deficit:,} behind",
        ),
        _game_board(
            "longest",
            "⏳",
            "Longest Games",
            matches,
            lambda m: m.battle_seconds,
            lambda m: _duration(m.battle_seconds),
        ),
        _game_board(
            "swings",
            "🎢",
            "Most Lead Changes",
            matches,
            lambda m: m.lead_changes or None,
            lambda m: f"{m.lead_changes} lead changes",
        ),
        _player_board("kills", "💀", "Most Kills in a Game", matches, lambda p: p.resources_killed),
        # The Best Trader award's own ratio, so a record and an award never
        # disagree about the same game. (Most units built was tried: every
        # row was a Zergling, which spawn in pairs.)
        _player_board("trades", "⚖️", "Best Trades", matches, trade_efficiency, lambda value: f"{value:.1f}x"),
    ]


# What each board answers to in `!records <category>`.
RECORD_ALIASES = {
    "comebacks": ("comeback", "comebacks", "cb"),
    "longest": ("long", "longest", "length", "duration"),
    "swings": ("swing", "swings", "lead", "leads", "back-and-forth", "rollercoaster"),
    "kills": ("kill", "kills", "killed"),
    "trades": ("trade", "trades", "trader", "efficiency"),
}


def board_index(query: str) -> int | None:
    """The page `!records <query>` opens on, or None if it names no board."""
    word = query.strip().lower()
    keys = list(RECORD_ALIASES)
    return next((keys.index(k) for k, words in RECORD_ALIASES.items() if word in words), None)
