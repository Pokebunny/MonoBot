r"""One-shot backfill: record which published version of the arcade map each
stored game was played on, then name those versions.

Games ingested before map_hash existed have an empty one. This fills them in
from a folder of replays — matching each replay to its stored game by
content_key (the same game recorded by different players still matches) — and
then interpolates the rest: the rotation changes rarely, so a game bracketed
in time by two replays on the same version was played on that version too.
Games astride a rotation change keep an empty hash and simply keep showing the
arcade map's name.

Usage (from repo root):
    uv run python scripts/backfill_map_hashes.py "<replay folder>" [name filter] [db path]

Example:
    uv run python scripts/backfill_map_hashes.py `
        "C:\Users\nrtab\OneDrive\Documents\StarCraft II\Accounts\85516\1-S2-1-539205\Replays\Multiplayer" `
        "monobattle lotv - map rotation"

On the server, main/resources/replays (every raw upload, named by hash) is the
folder with the best overlap with stored games; a personal replay folder works
too and covers games uploaded by other people.

Re-running is safe: it only touches games that still have no version, and a
map version is fetched from Blizzard's depot once and cached in the DB.
"""

import bisect
import glob
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "main"))

import sc2reader  # noqa: E402
from services import map_versions  # noqa: E402
from services.storage import DEFAULT_DB_PATH, MatchStore, content_key_for  # noqa: E402


def _archive_samples(paths: list[str]) -> list[tuple[str, str, str]]:
    """(content_key, played_at, map_hash) for each readable replay. Level 2 is
    all this needs — the roster and start time that identify the game, plus
    the map hash — so it runs in a fraction of a full parse."""
    samples = []
    for i, path in enumerate(paths):
        try:
            replay = sc2reader.load_replay(path, load_level=2)
            key = content_key_for(replay.start_time, [p.name for p in replay.players])
            samples.append((key, replay.start_time.isoformat(), replay.map_hash))
        except Exception as e:
            print(f"  SKIPPED {os.path.basename(path)}: {type(e).__name__}: {e}")
        if (i + 1) % 100 == 0:
            print(f"  ...read {i + 1}/{len(paths)}")
    return samples


def _bracketed_hash(played_at: str, times: list[str], hashes: list[str]) -> str | None:
    """The version live at played_at, if the nearest known game on either side
    agrees. Disagreement means a rotation happened in between — unknowable
    from timestamps alone, so leave it be."""
    i = bisect.bisect_left(times, played_at)
    if i == 0 or i >= len(times):
        return None  # outside the sampled range entirely
    return hashes[i - 1] if hashes[i - 1] == hashes[i] else None


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    folder = sys.argv[1]
    name_filter = sys.argv[2].lower() if len(sys.argv) > 2 else ""
    db_path = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_DB_PATH

    paths = sorted(
        p for p in glob.glob(os.path.join(folder, "*.SC2Replay")) if name_filter in os.path.basename(p).lower()
    )
    store = MatchStore(db_path)
    missing = store.match_ids_missing_map_hash()
    print(f"{len(missing)} stored games have no map version; reading {len(paths)} replays")
    if not missing:
        print("nothing to backfill")
    samples = _archive_samples(paths)

    by_key = {key: map_hash for key, _, map_hash in samples}
    exact = 0
    for match_id, _, content_key in missing:
        map_hash = by_key.get(content_key)
        if map_hash:
            store.set_map_hash(match_id, map_hash)
            exact += 1

    # Timeline for interpolation: everything now known, from the DB (which now
    # includes the exact matches above) and from replays that aren't stored.
    timeline = sorted(set(store.map_hash_samples()) | {(t, h) for _, t, h in samples})
    times = [t for t, _ in timeline]
    hashes = [h for _, h in timeline]
    inferred = 0
    for match_id, played_at, _ in store.match_ids_missing_map_hash():
        map_hash = _bracketed_hash(played_at, times, hashes)
        if map_hash:
            store.set_map_hash(match_id, map_hash)
            inferred += 1

    left = len(store.match_ids_missing_map_hash())
    print(f"versions set: {exact} from a matching replay, {inferred} inferred from surrounding games, {left} unknown")

    print("resolving map names (one download per version)...")
    for version in map_versions.resolve_pending(store):
        by = f" by {version.author}" if version.author else ""
        print(f"  {version.map_hash[:12]} = {version.name}{by}")
    names = store.map_version_names()
    print(f"{len(names)} map versions named")


if __name__ == "__main__":
    main()
