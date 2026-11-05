"""The real map behind a game (see CLAUDE.md "map"). Single-terrain arcade
maps (e.g. "Monobattle LotV - Kimeran Refuge") are named from the arcade name.
"""

import asyncio
import logging
from collections.abc import Iterable

from models.replay import MapVersion, MonobattleMatch
from services import replay_parser

logger = logging.getLogger(__name__)

ROTATION_MAP = "Monobattle LotV - Map Rotation"
_ARCADE_PREFIXES = ("Monobattle LotV - ", "Monobattle - ")


def resolve_pending(store) -> list[MapVersion]:
    """Fetch and cache every unfetched map version (failures too); returns the
    ones that named a map. Blocking, for scripts — the bot uses
    resolve_pending_async (sqlite connections are thread-bound)."""
    resolved = []
    for map_hash in store.unresolved_map_hashes():
        version = replay_parser.fetch_map_version(map_hash)
        if version is None:
            store.record_map_version(map_hash, None)
            continue
        store.record_map_version(map_hash, version)
        logger.info("Map version %s is %s", map_hash[:12], version.name)
        resolved.append(version)
    return resolved


def label(match: MonobattleMatch, names: dict[str, str]) -> str:
    """The terrain when known, else the arcade name."""
    return named(match, names) or match.map_name


async def resolve_pending_async(store) -> list[MapVersion]:
    """resolve_pending with only the download off the event-loop thread."""
    resolved = []
    for map_hash in store.unresolved_map_hashes():
        version = await asyncio.to_thread(replay_parser.fetch_map_version, map_hash)
        store.record_map_version(map_hash, version)
        if version is not None:
            logger.info("Map version %s is %s", map_hash[:12], version.name)
            resolved.append(version)
    return resolved


def canonical(name: str) -> str:
    """The spelling a map is grouped under. Identity today; the single place
    an alias table would go."""
    return name


def named(match: MonobattleMatch, names: dict[str, str]) -> str | None:
    """The map played on, or None when unknown (never the rotation's arcade
    name)."""
    name = names.get(match.map_hash) or fixed_map(match.map_name)
    return canonical(name) if name else None


def fixed_map(arcade_name: str) -> str | None:
    """A single-terrain arcade map's name minus its prefix; None for the
    rotation map."""
    if not arcade_name or arcade_name == ROTATION_MAP:
        return None
    for prefix in _ARCADE_PREFIXES:
        if arcade_name.startswith(prefix) and len(arcade_name) > len(prefix):
            return arcade_name.removeprefix(prefix)
    return arcade_name


def group_by_map(matches: Iterable[MonobattleMatch], names: dict[str, str]) -> dict[str, list[MonobattleMatch]]:
    """Games by map name, newest map first; unknown maps are left out."""
    groups: dict[str, list[MonobattleMatch]] = {}
    for match in matches:
        name = named(match, names)
        if name is not None:
            groups.setdefault(name, []).append(match)
    return dict(sorted(groups.items(), key=lambda kv: max(m.played_at for m in kv[1]), reverse=True))
