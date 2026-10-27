"""One-shot backfill: record the game seed of games stored before
matches.game_seed existed, so dedupe recognises another recording of them by
seed rather than by start time and lineup (storage._same_game).

Reads only each archived replay's init data and sets ONLY game_seed on the
game stored from the same file (matched by file hash). Everything else stays
as stored, content_key included -- a game's identity doesn't move.

Usage (from repo root):
    uv run python scripts/backfill_seeds.py [archive_dir]

archive_dir defaults to main/resources/replays (where the bot archives
uploads). Set MONOBOT_DB to backfill a database other than the home one.
Re-running is safe: games that already have a seed are left alone. Nothing
derived reads the seed, so the bot needn't restart.
"""

import glob
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "main"))

from services import replay_parser  # noqa: E402
from services.storage import MatchStore  # noqa: E402

DEFAULT_ARCHIVE = os.path.join(os.path.dirname(__file__), "..", "main", "resources", "replays")


def main() -> None:
    archive = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ARCHIVE
    store = MatchStore(os.environ["MONOBOT_DB"]) if os.environ.get("MONOBOT_DB") else MatchStore()
    stored = store.file_hashes()
    paths = [p for p in sorted(glob.glob(os.path.join(archive, "*.SC2Replay"))) if os.path.basename(p)[:-10] in stored]
    print(f"{len(paths)} archived replays of stored games")

    seeds: dict[str, int] = {}
    failed = 0
    for i, path in enumerate(paths):
        try:
            seed = replay_parser.read_game_seed(path)
        except Exception as e:
            print(f"  unreadable: {os.path.basename(path)}: {e}")
            failed += 1
            continue
        if seed is not None:
            seeds[os.path.basename(path)[:-10]] = seed
        if (i + 1) % 200 == 0:
            print(f"  {i + 1}/{len(paths)}...")

    print(f"seeds read={len(seeds)} failed={failed} games updated={store.set_game_seeds(seeds)}")


if __name__ == "__main__":
    main()
