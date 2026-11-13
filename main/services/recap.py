"""Session recaps. A session is a run of games with no gap over SESSION_GAP
between starts."""

import datetime

from models.recap import PlayerRecap, SessionRecap
from models.replay import MonobattleMatch
from services.achievements import RARITIES, SPECS_BY_KEY
from services.rating import DEFAULT_DISPLAY, DEFAULT_RAW, MIN_WINNER_CONFIDENCE, RatingBook, long_enough

SESSION_GAP = datetime.timedelta(hours=3)


def split_sessions(matches: list[tuple[int, MonobattleMatch]]) -> list[list[tuple[int, MonobattleMatch]]]:
    """Every session's matches, oldest session first and each oldest first.
    `matches` is oldest first, as MatchStore.all_matches returns them."""
    sessions: list[list[tuple[int, MonobattleMatch]]] = []
    for item in matches:
        if sessions and item[1].played_at - sessions[-1][-1][1].played_at <= SESSION_GAP:
            sessions[-1].append(item)
        else:
            sessions.append([item])
    return sessions


def latest_session(matches: list[tuple[int, MonobattleMatch]]) -> list[tuple[int, MonobattleMatch]]:
    """The most recent session's matches, oldest first. `matches` is oldest
    first, as MatchStore.all_matches returns them."""
    sessions = split_sessions(matches)
    return sessions[-1] if sessions else []


def is_decided(match: MonobattleMatch) -> bool:
    """Counts toward a recap's W/L and MVPs: the same gate profile records use."""
    return match.winning_team is not None and match.winner_confidence >= MIN_WINNER_CONFIDENCE and long_enough(match)


def build_recap(
    session: list[tuple[int, MonobattleMatch]],
    season_matches: list[tuple[int, MonobattleMatch]],
    merge_map: dict[str, str],
    unlocks: list[tuple[str, str]],
    start: RatingBook | None = None,
    raw: bool = False,
) -> SessionRecap | None:
    """Summarise `session` (oldest first); None if nothing was decided.
    `season_matches` drive the before/after ratings; `start`
    (rating.season_start) is consumed. `raw` reports raw_rating, not the
    hedged display_rating."""
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

    shown = (lambda r: r.raw_rating) if raw else (lambda r: r.display_rating)  # noqa: E731
    fresh = DEFAULT_RAW if raw else DEFAULT_DISPLAY
    session_ids = {mid for mid, _ in session}
    last_id = session[-1][0]
    book = start if start is not None else RatingBook(merge_map)
    for mid, match in sorted(season_matches, key=lambda im: im[1].played_at):
        if mid in session_ids and book.is_rateable(match):
            for p in match.players:
                entry = players.get(canonical(p.toon_handle))
                if entry is not None and entry.rating_before is None:
                    rating = book.standing_for(p.toon_handle)
                    entry.rating_before = shown(rating) if rating is not None else fresh
        book.rate_match(match)
        if mid == last_id:
            break
    for entry in players.values():
        if entry.rating_before is not None:
            entry.rating_after = shown(book.rating_for(entry.handle))

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
        raw=raw,
    )
