"""Delete stored games that ingest would now turn away: anything that isn't a
4v4, and remakes (see storage.rejection_reason). Lists them, and deletes only
with --apply.

Usage (from repo root):
    uv run python scripts/prune_matches.py            # dry run
    uv run python scripts/prune_matches.py --apply

MONOBOT_DB picks the database (default: the home one). The bot announces any
achievement a deletion takes away at its next restart (the startup reconcile),
so restart it once after pruning rather than leaving it to a later deploy.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "main"))

from services import storage  # noqa: E402
from services.storage import MatchStore  # noqa: E402


def main() -> None:
    apply = "--apply" in sys.argv[1:]
    store = MatchStore(os.environ["MONOBOT_DB"]) if os.environ.get("MONOBOT_DB") else MatchStore()
    doomed = []
    for match_id, match in store.all_matches():
        reason = storage.rejection_reason(match)
        if reason is not None:
            doomed.append(match_id)
            print(f"  #{match_id} {match.played_at:%Y-%m-%d} {match.battle_seconds}s: {reason}")
    if not apply:
        print(f"{len(doomed)} games would be deleted; re-run with --apply")
        return
    deleted = store.delete_matches(doomed)
    print(f"deleted {deleted} games; database now holds {store.match_count()} matches")


if __name__ == "__main__":
    main()
