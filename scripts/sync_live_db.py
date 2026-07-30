"""Pull a snapshot of the LIVE database off the server to this machine.

The server's `monobot.db` is the real one — it holds the player_links and
merges that the local copy lacks. This takes a consistent snapshot (sqlite's
online backup API, safe while the bot is writing), copies it down, verifies it,
and only then swaps it into place, so an interrupted run never leaves a
truncated file behind.

Usage (from repo root):
    uv run python scripts/sync_live_db.py [--dest PATH] [--keep N] [--quiet]

Analysis scripts read the result via the MONOBOT_DB env var, e.g.
    MONOBOT_DB=main/resources/monobot-live.db uv run python scripts/mu_leaderboard.py

Designed to be run unattended on a schedule: it logs one line per run to
`<dest>.log`, exits 0 on success and non-zero on any failure.
"""

import argparse
import datetime
import logging
import os
import shutil
import sqlite3
import subprocess
import sys

SSH_HOST = "monobot@52.14.192.236"
SSH_KEY = os.path.expanduser("~/.ssh/monobot_deploy")
REMOTE_DB = "/opt/monobot/MonoBot/main/resources/monobot.db"
REMOTE_SNAPSHOT = "/tmp/monobot-sync-snapshot.db"
DEFAULT_DEST = os.path.join(os.path.dirname(__file__), "..", "main", "resources", "monobot-live.db")

# The server runs Python 3.9 and has no sqlite3 CLI, so the snapshot is taken
# with a here-doc through the stdlib. src is opened read-only; backup() copies
# under a shared lock, so an in-flight bot write can't tear the file.
SNAPSHOT_SCRIPT = f"""
import sqlite3
src = sqlite3.connect("file:{REMOTE_DB}?mode=ro", uri=True)
dst = sqlite3.connect("{REMOTE_SNAPSHOT}")
with dst:
    src.backup(dst)
dst.close()
src.close()
print(open("{REMOTE_SNAPSHOT}", "rb").seek(0, 2))
"""

logger = logging.getLogger("sync_live_db")


def run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({result.returncode}): {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def verify(path: str) -> tuple[int, int]:
    """Open the downloaded file and prove it is a sound database, not a
    half-written one. Returns (matches, player_links) for the log line."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        ok = conn.execute("pragma integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"integrity_check said {ok!r}")
        matches = conn.execute("select count(*) from matches").fetchone()[0]
        links = conn.execute("select count(*) from player_links").fetchone()[0]
        if matches == 0:
            raise RuntimeError("snapshot has zero matches — refusing to install it")
        return matches, links
    finally:
        conn.close()


def rotate(dest: str, keep: int) -> None:
    """Keep the previous N snapshots alongside the current one, newest first,
    so a bad sync (or a bad day on the server) is recoverable."""
    if keep <= 0 or not os.path.exists(dest):
        return
    stamp = datetime.datetime.fromtimestamp(os.path.getmtime(dest)).strftime("%Y%m%d-%H%M%S")
    base, ext = os.path.splitext(dest)
    shutil.copy2(dest, f"{base}.{stamp}{ext}")
    history = sorted(
        f for f in os.listdir(os.path.dirname(dest) or ".") if f.startswith(os.path.basename(base) + ".") and f.endswith(ext)
    )
    for old in history[:-keep]:
        os.remove(os.path.join(os.path.dirname(dest) or ".", old))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default=DEFAULT_DEST, help="where to write the snapshot")
    ap.add_argument("--keep", type=int, default=7, help="how many previous snapshots to retain (0 = none)")
    ap.add_argument("--quiet", action="store_true", help="log to file only, no console output")
    args = ap.parse_args()

    dest = os.path.abspath(args.dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(dest + ".log", encoding="utf-8")]
    if not args.quiet:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)

    tmp = dest + ".part"
    try:
        run(["ssh", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", SSH_HOST, f"python3 - <<'PY'{SNAPSHOT_SCRIPT}PY"])
        run(["scp", "-i", SSH_KEY, "-o", "BatchMode=yes", "-q", f"{SSH_HOST}:{REMOTE_SNAPSHOT}", tmp])
        matches, links = verify(tmp)
        rotate(dest, args.keep)
        os.replace(tmp, dest)  # atomic: readers see either the old file or the new one
        size = os.path.getsize(dest)
        logger.info("synced %s (%.1f KB, %d matches, %d player_links)", dest, size / 1024, matches, links)
        return 0
    except Exception as exc:  # noqa: BLE001 — unattended run: log and signal failure
        logger.error("sync failed: %s: %s", type(exc).__name__, exc)
        return 1
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
        subprocess.run(
            ["ssh", "-i", SSH_KEY, "-o", "BatchMode=yes", SSH_HOST, f"rm -f {REMOTE_SNAPSHOT}"],
            capture_output=True,
        )


if __name__ == "__main__":
    sys.exit(main())
