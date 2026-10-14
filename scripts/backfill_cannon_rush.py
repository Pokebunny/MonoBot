"""One-shot backfill: record who cannon rushed in games stored before
match_players.cannon_rush existed.

Re-parses each archived replay and sets ONLY that column on the game stored
from the same file (matched by file hash). Everything else stays as stored —
scripts/reparse_stored.py would also apply every other parser change since
each game was ingested, which can move winners and picks.

Usage (from repo root):
    uv run python scripts/backfill_cannon_rush.py [archive_dir]

archive_dir defaults to main/resources/replays (where the bot archives
uploads). Set MONOBOT_DB to backfill a database other than the default one.
Re-running is safe; it just sets the same values again. The bot caches
derived boards per process, so restart it afterwards to pick the flags up.
"""

import glob
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "main"))

from services import replay_parser, storage  # noqa: E402
from services.storage import MatchStore  # noqa: E402

DEFAULT_ARCHIVE = os.path.join(os.path.dirname(__file__), "..", "main", "resources", "replays")


def main() -> None:
    archive = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ARCHIVE
    paths = sorted(glob.glob(os.path.join(archive, "*.SC2Replay")))
    print(f"{len(paths)} archived replays")

    store = MatchStore(os.environ["MONOBOT_DB"]) if os.environ.get("MONOBOT_DB") else MatchStore()
    updated = not_stored = failed = rushes = 0
    for i, path in enumerate(paths):
        with open(path, "rb") as f:
            file_hash = storage.hash_replay(f.read())
        try:
            match = replay_parser.parse_replay(path)
        except Exception as e:
            print(f"  parse failed: {os.path.basename(path)}: {e}")
            failed += 1
            continue
        if store.set_cannon_rush(file_hash, {p.toon_handle: p.cannon_rush for p in match.players}):
            updated += 1
            rushes += sum(1 for p in match.players if p.cannon_rush)
        else:
            not_stored += 1
        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(paths)}...")

    print(f"updated={updated} not_stored={not_stored} failed={failed} cannon_rushes={rushes}")


if __name__ == "__main__":
    main()
