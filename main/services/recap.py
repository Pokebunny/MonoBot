"""Session recaps: who played, how they did, and what moved, over a stretch of
games — the latest session on demand, or the last 24 hours for the morning
post.

A "session" is a run of games with no gap longer than SESSION_GAP between
consecutive starts. Monobattle nights are dense (a game every 15-30 minutes)
and separated by most of a day, so the split is unambiguous in practice.
"""

import datetime

from models.recap import PlayerRecap, SessionRecap
from models.replay import MonobattleMatch
from services.achievements import RARITIES, SPECS_BY_KEY
from services.rating import DEFAULT_DISPLAY, MIN_DURATION_SECONDS, MIN_WINNER_CONFIDENCE, RatingBook

SESSION_GAP = datetime.timedelta(hours=3)


def latest_session(matches: list[tuple[int, MonobattleMatch]]) -> list[tuple[int, MonobattleMatch]]:
    """The most recent session's matches, oldest first. `matches` is oldest
    first, as MatchStore.all_matches returns them."""
    if not matches:
        return []
    start = len(matches) - 1
    while start > 0 and matches[start][1].played_at - matches[start - 1][1].played_at <= SESSION_GAP:
        start -= 1
    return matches[start:]


def is_decided(match: MonobattleMatch) -> bool:
    """Counts toward a recap's W/L and MVPs: the same gate profile records use."""
    return (
        match.winning_team is not None
        and match.winner_confidence >= MIN_WINNER_CONFIDENCE
        and match.duration_seconds >= MIN_DURATION_SECONDS
    )


def build_recap(
    session: list[tuple[int, MonobattleMatch]],
    season_matches: list[tuple[int, MonobattleMatch]],
    merge_map: dict[str, str],
    unlocks: list[tuple[str, str]],
) -> SessionRecap | None:
    """Summarise `session` (oldest first). None when none of its games were
    decided — there is nothing to report.

    `season_matches` are the season the session was played in, for the rating
    walk: each player's "before" is their rating going into their first game
    of the session, and "after" their rating coming out of the last one.
    `unlocks` are (handle, key) ledger rows earned in the session's window."""
    decided = [m for _, m in session if is_decided(m)]
    if not decided:
        return None
    canonical = lambda h: merge_map.get(h, h)  # noqa: E731

    players: dict[str, PlayerRecap] = {}
    for match in decided:
        mvp = match.mvp()
        for p in match.players:
            handle = canonical(p.toon_handle)
            entry = players.setdefault(handle, PlayerRecap(handle=handle, name=p.name))
            entry.name = p.name  # latest name wins
            if p.team == match.winning_team:
                entry.wins += 1
            else:
                entry.losses += 1
            if p is mvp:
                entry.mvps += 1

    session_ids = {mid for mid, _ in session}
    last_id = session[-1][0]
    book = RatingBook(merge_map)
    for mid, match in sorted(season_matches, key=lambda im: im[1].played_at):
        if mid in session_ids and book.is_rateable(match):
            for p in match.players:
                entry = players.get(canonical(p.toon_handle))
                if entry is not None and entry.rating_before is None:
                    rating = book.rating_for(p.toon_handle)
                    entry.rating_before = rating.display_rating if rating is not None else DEFAULT_DISPLAY
        book.rate_match(match)
        if mid == last_id:
            break
    for entry in players.values():
        if entry.rating_before is not None:
            entry.rating_after = book.rating_for(entry.handle).display_rating

    for handle, key in unlocks:
        entry = players.get(canonical(handle))
        if entry is not None and key in SPECS_BY_KEY and key not in entry.achievement_keys:
            entry.achievement_keys.append(key)
    for entry in players.values():
        entry.achievement_keys.sort(key=lambda k: -RARITIES.index(SPECS_BY_KEY[k].rarity))

    return SessionRecap(
        started_at=session[0][1].played_at,
        ended_at=session[-1][1].played_at,
        games=len(decided),
        players=list(players.values()),
    )
