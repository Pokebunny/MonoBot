"""Resolving the real map behind a monobattle.

Every game is played on one arcade map, so a match's map_name is always
"Monobattle LotV - Map Rotation". The rotation happens out-of-band: the author
republishes the arcade map with new terrain, which changes its hash. The
terrain's real name is only inside the published map file, so it is fetched
once per hash (network) and cached in the DB — see
replay_parser.fetch_map_version and MatchStore.record_map_version.
"""

import asyncio
import logging
from collections.abc import Iterable

from models.replay import MapVersion, MonobattleMatch
from services import replay_parser

logger = logging.getLogger(__name__)


def resolve_pending(store) -> list[MapVersion]:
    """Look up every map version seen in history that has never been fetched,
    caching each result (including failures). Returns the ones that named a
    map. A version is fetched exactly once, and an unreachable depot just
    leaves it unresolved for next time.

    Blocking, for scripts. The bot must use resolve_pending_async instead:
    this touches the store, and a MatchStore's sqlite connection belongs to
    the thread that created it, so this cannot be run via asyncio.to_thread."""
    resolved = []
    for map_hash in store.unresolved_map_hashes():
        version = replay_parser.fetch_map_version(map_hash)
        if version is None:
            # Depot failures and mute headers both cache as "no name"; the
            # only cost of a wrong guess here is a match labelled with the
            # arcade name, which is what it showed before anyway.
            store.record_map_version(map_hash, None)
            continue
        store.record_map_version(map_hash, version)
        logger.info("Map version %s is %s", map_hash[:12], version.name)
        resolved.append(version)
    return resolved


def label(match: MonobattleMatch, names: dict[str, str]) -> str:
    """What to call this game's map on screen: the terrain when known, else
    the arcade map's own name (which is all older, un-backfilled matches
    have)."""
    return named(match, names) or match.map_name


async def resolve_pending_async(store) -> list[MapVersion]:
    """resolve_pending for the bot: only the depot download goes to a worker
    thread. The DB calls stay on the event-loop thread, because that is where
    the store's sqlite connection was created and sqlite refuses to be used
    from any other one."""
    resolved = []
    for map_hash in store.unresolved_map_hashes():
        version = await asyncio.to_thread(replay_parser.fetch_map_version, map_hash)
        store.record_map_version(map_hash, version)
        if version is not None:
            logger.info("Map version %s is %s", map_hash[:12], version.name)
            resolved.append(version)
    return resolved


def canonical(name: str) -> str:
    """The one spelling a map is displayed and grouped under. The author
    republishes the same terrain under a new hash from time to time, and the
    names have always matched exactly, so this is identity today — it exists
    so that grouping goes through a single place if a version ever spells a
    map differently (an alias table would slot in here, not at every caller).
    """
    return name


def named(match: MonobattleMatch, names: dict[str, str]) -> str | None:
    """The map this game was played on, or None when it isn't known — the
    version was never resolved, or the game predates map hashes. Stats group
    on this; the arcade name is a placeholder, not a map, so it never becomes
    a bucket of its own."""
    name = names.get(match.map_hash)
    return canonical(name) if name else None


def group_by_map(matches: Iterable[MonobattleMatch], names: dict[str, str]) -> dict[str, list[MonobattleMatch]]:
    """Games bucketed by the map they were played on, newest map first, for
    per-map stats. Games on an unidentified map are left out entirely (count
    them as the shortfall against the input if a caller needs to say so) —
    two versions of one map land in the same bucket, since they resolve to
    the same name."""
    groups: dict[str, list[MonobattleMatch]] = {}
    for match in matches:
        name = named(match, names)
        if name is not None:
            groups.setdefault(name, []).append(match)
    return dict(sorted(groups.items(), key=lambda kv: max(m.played_at for m in kv[1]), reverse=True))
