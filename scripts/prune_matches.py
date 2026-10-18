"""Delete stored games that ingest would now turn away: anything that isn't a
4v4, and remakes (see storage.rejection_reason). Lists them, and deletes only
with --apply.

Usage (from repo root):
    uv run python scripts/prune_matches.py [db ...]                   # dry run
    uv run python scripts/prune_matches.py [db ...] --apply [--files]

The databases default to the home one (or MONOBOT_DB). Pass every community's
database together when using --files: it also deletes raw replays from the
archive (main/resources/replays) that fail the rule. A file still held by any
of the given databases is kept without parsing; the rest are parsed, since an
archived file is often a second recording of a game stored under another
player's file, and its hash says nothing about which game it is.

The bot announces any achievement a deletion takes away at its next restart
(the startup reconcile), so restart it once after pruning rather than leaving
it to a later deploy.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "main"))

from services import replay_parser, storage  # noqa: E402
from services.storage import MatchStore  # noqa: E402

ARCHIVE_DIR = os.path.join(os.path.dirname(__file__), "..", "main", "resources", "replays")


def prune_db(store: MatchStore, apply: bool) -> None:
    doomed = []
    for match_id, match in store.all_matches():
        reason = storage.rejection_reason(match)
        if reason is not None:
            doomed.append(match_id)
            print(f"  #{match_id} {match.played_at:%Y-%m-%d} {match.battle_seconds}s: {reason}")
    if not apply:
        print(f"{len(doomed)} games would be deleted")
        return
    deleted = store.delete_matches(doomed)
    print(f"deleted {deleted} games; database now holds {store.match_count()} matches")


def prune_archive(stores: list[MatchStore], apply: bool) -> None:
    kept = set().union(*(store.file_hashes() for store in stores))
    names = sorted(n for n in os.listdir(ARCHIVE_DIR) if n.lower().endswith(".sc2replay"))
    doomed = []
    for name in names:
        if name.removesuffix(".SC2Replay") in kept:
            continue
        path = os.path.join(ARCHIVE_DIR, name)
        try:
            reason = storage.rejection_reason(replay_parser.parse_replay(path))
        except Exception as e:
            print(f"  could not parse {name}, keeping it: {type(e).__name__}")
            continue
        if reason is not None:
            doomed.append(path)
    verb = "deleted" if apply else "would delete"
    print(f"archive: {verb} {len(doomed)} of {len(names)} files")
    if apply:
        for path in doomed:
            os.remove(path)


def main() -> None:
    args = sys.argv[1:]
    apply, files = "--apply" in args, "--files" in args
    paths = [a for a in args if not a.startswith("--")]
    if not paths:
        paths = [os.environ["MONOBOT_DB"]] if os.environ.get("MONOBOT_DB") else [storage.DEFAULT_DB_PATH]
    stores = [MatchStore(path) for path in paths]
    for path, store in zip(paths, stores):
        print(f"== {path}")
        prune_db(store, apply)
    if files:
        prune_archive(stores, apply)
    if not apply:
        print("dry run; re-run with --apply")


if __name__ == "__main__":
    main()
