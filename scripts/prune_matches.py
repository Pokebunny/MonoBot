"""Delete stored games that ingest would now turn away: anything that isn't a
4v4, and remakes (see storage.rejection_reason), except games someone approved
on upload. Lists them, and deletes only with --apply.

Usage (from repo root):
    uv run python scripts/prune_matches.py [db ...]                   # dry run
    uv run python scripts/prune_matches.py [db ...] --apply [--files]

The databases default to the home one (or MONOBOT_DB). --files also deletes
every raw replay in the archive (main/resources/replays) that no database
stores: a game turned away, or the weaker of two recordings of one game. The
archive is shared, so this checks every database under main/resources (each
community's, not backups) as well as the ones given.

The bot announces any achievement a deletion takes away at its next restart
(the startup reconcile), so restart it once after pruning rather than leaving
it to a later deploy.
"""

import glob
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "main"))

from services import storage  # noqa: E402
from services.storage import MatchStore  # noqa: E402

RESOURCES_DIR = os.path.join(os.path.dirname(__file__), "..", "main", "resources")
ARCHIVE_DIR = os.path.join(RESOURCES_DIR, "replays")


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


def every_database(given: list[str]) -> list[str]:
    """The given databases plus every live one under main/resources: the
    home ladder and each community's (guilds/<id>/). Not backups
    (monobot-*.db): a snapshot doesn't keep a file the bot no longer stores."""
    found = [os.path.join(RESOURCES_DIR, "monobot.db")]
    found += glob.glob(os.path.join(RESOURCES_DIR, "guilds", "*", "monobot.db"))
    return sorted({os.path.abspath(p) for p in given + found if os.path.exists(p)})


def prune_archive(paths: list[str], apply: bool) -> None:
    kept = set()
    for path in every_database(paths):
        store = MatchStore(path)
        kept |= store.file_hashes()
        store.close()
        print(f"  keeping files stored in {path}")
    names = sorted(n for n in os.listdir(ARCHIVE_DIR) if n.lower().endswith(".sc2replay"))
    doomed = [os.path.join(ARCHIVE_DIR, n) for n in names if n.removesuffix(".SC2Replay") not in kept]
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
        prune_archive(paths, apply)
    if not apply:
        print("dry run; re-run with --apply")


if __name__ == "__main__":
    main()
