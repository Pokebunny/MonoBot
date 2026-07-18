"""Re-parse every archived replay file and refresh its stored match in place.

Run after a parser improvement to apply it retroactively. Only touches games
already in the database (matched by file hash); manually confirmed winners
are preserved. Files that aren't stored are reported, not ingested — keeping
the community ladder curated.

Usage (from repo root):
    uv run python scripts/reparse_stored.py [archive_dir]

archive_dir defaults to main/resources/replays (where the bot archives
uploads).
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

    store = MatchStore()
    refreshed = not_stored = failed = 0
    for i, path in enumerate(paths):
        with open(path, "rb") as f:
            file_hash = storage.hash_replay(f.read())
        try:
            match = replay_parser.parse_replay(path)
        except Exception as e:
            print(f"  parse failed: {os.path.basename(path)}: {e}")
            failed += 1
            continue
        if store.refresh_parse(match, file_hash):
            refreshed += 1
        else:
            not_stored += 1
        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(paths)}...")

    print(f"refreshed={refreshed} not_stored={not_stored} failed={failed}")


if __name__ == "__main__":
    main()
