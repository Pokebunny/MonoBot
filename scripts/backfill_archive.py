"""One-shot backfill: parse a folder of replays into the match database.

Usage (from repo root):
    uv run python scripts/backfill_archive.py "<replay folder>" [name filter]

Example:
    uv run python scripts/backfill_archive.py `
        "C:\\Users\\nrtab\\OneDrive\\Documents\\StarCraft II\\Accounts\\85516\\1-S2-1-539205\\Replays\\Multiplayer" `
        "monobattle lotv - map rotation"

Re-running is safe: files already in the database are skipped by hash.
"""

import glob
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "main"))

from services import replay_parser, storage  # noqa: E402
from services.storage import MatchStore  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    folder = sys.argv[1]
    name_filter = sys.argv[2].lower() if len(sys.argv) > 2 else ""

    paths = sorted(
        p for p in glob.glob(os.path.join(folder, "*.SC2Replay")) if name_filter in os.path.basename(p).lower()
    )
    print(f"{len(paths)} replays to ingest")

    store = MatchStore()
    ingested = skipped = failed = 0
    for i, path in enumerate(paths):
        try:
            with open(path, "rb") as f:
                file_hash = storage.hash_replay(f.read())
            if store.has_replay(file_hash):
                skipped += 1
                continue
            match = replay_parser.parse_replay(path)
            store.ingest(match, file_hash, uploaded_by="backfill")
            ingested += 1
        except Exception as e:
            failed += 1
            print(f"  FAILED {os.path.basename(path)}: {type(e).__name__}: {e}")
        if (i + 1) % 50 == 0:
            print(f"  ...{i + 1}/{len(paths)}")

    print(f"done: {ingested} ingested, {skipped} already stored, {failed} failed")
    print(f"database now holds {store.match_count()} matches")


if __name__ == "__main__":
    main()
