"""Populate the pubs database — the FULL game archive (community games
included) kept separate from the curated community ladder (monobot.db).

The community DB drives rankings and matchmaking off a small, clean set of
games. pubs.db instead keeps everything, so aggregate stats (unit win rates)
draw on the much larger sample. Pub games don't move community ratings.

Searches the folder recursively (subfolders included).

Usage (from repo root):
    uv run python scripts/split_pubs.py "<replay folder>" [name filter]

Re-running is safe: pubs.db dedupes by hash/content the same as the main DB.
"""

import glob
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "main"))

from services import replay_parser, storage  # noqa: E402
from services.storage import MatchStore  # noqa: E402

PUBS_DB = os.path.join(os.path.dirname(__file__), "..", "main", "resources", "pubs.db")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    folder = sys.argv[1]
    name_filter = sys.argv[2].lower() if len(sys.argv) > 2 else ""

    pubs = MatchStore(PUBS_DB)
    paths = sorted(
        p
        for p in glob.glob(os.path.join(folder, "**", "*.SC2Replay"), recursive=True)
        if name_filter in os.path.basename(p).lower()
    )
    counts: Counter = Counter()
    for i, path in enumerate(paths):
        try:
            with open(path, "rb") as f:
                file_hash = storage.hash_replay(f.read())
            if pubs.has_replay(file_hash):
                counts["dup"] += 1
                continue
            match = replay_parser.parse_replay(path)
            pubs.ingest(match, file_hash, uploaded_by="pub-backfill")
            counts["added"] += 1
        except Exception as e:
            counts["failed"] += 1
            print(f"  FAILED {os.path.basename(path)}: {type(e).__name__}: {e}")
        if (i + 1) % 50 == 0:
            print(f"  ...{i + 1}/{len(paths)}")

    print(f"done: {counts['added']} added, {counts['dup']} already present, {counts['failed']} failed")
    print(f"pubs.db now holds {pubs.match_count()} games")


if __name__ == "__main__":
    main()
