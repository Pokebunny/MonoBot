"""Watch the SC2 replay folders and post new monobattle replays to MonoBot's
upload webhook. Standard library only, so it runs on any Python 3.10+.

    python monobot_uploader.py --webhook https://discord.com/api/webhooks/...   # first run saves it
    python monobot_uploader.py                                                  # then just this

The bot decides which server a game belongs to; this only finds and sends files.
See README.md."""

import argparse
import glob
import hashlib
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

logger = logging.getLogger("monobot_uploader")

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".monobot_uploader.json")
# SC2 names a saved replay after its map, and every monobattle is played on
# one arcade map whose name starts with this.
DEFAULT_NAME_PREFIX = "Monobattle"
POLL_SECONDS = 10
# SC2 writes the file at game end; wait until it has stopped changing.
SETTLE_SECONDS = 5
# Past Discord's 10MB upload limit for a webhook (replays are ~0.1-2MB).
MAX_BYTES = 10 * 1024 * 1024
RETRY_DELAYS = (5, 30, 120)


def default_replay_roots() -> list[str]:
    """The Accounts folders SC2 uses on Windows and macOS."""
    home = os.path.expanduser("~")
    roots = [
        os.path.join(home, "Documents", "StarCraft II", "Accounts"),
        os.path.join(home, "OneDrive", "Documents", "StarCraft II", "Accounts"),
        os.path.join(home, "Library", "Application Support", "Blizzard", "StarCraft II", "Accounts"),
    ]
    one_drive = os.environ.get("OneDrive")
    if one_drive:
        roots.append(os.path.join(one_drive, "Documents", "StarCraft II", "Accounts"))
    return list(dict.fromkeys(roots))


def replay_folders(roots: list[str]) -> list[str]:
    """Every account's Multiplayer replay folder under the given Accounts roots
    (Accounts/<account>/<region-realm-toon>/Replays/Multiplayer)."""
    folders = []
    for root in roots:
        folders += glob.glob(os.path.join(root, "*", "*", "Replays", "Multiplayer"))
    return sorted(set(folders))


def is_monobattle(path: str, prefix: str) -> bool:
    name = os.path.basename(path)
    return name.lower().endswith(".sc2replay") and name.lower().startswith(prefix.lower())


def load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


class Uploader:
    def __init__(self, config: dict, config_path: str = CONFIG_PATH, post=None):
        self.config = config
        self.config_path = config_path
        self.post = post or post_to_webhook
        self.uploaded: set[str] = set(config.get("uploaded", []))
        self.sizes: dict[str, int] = {}  # path -> size at the last scan

    def folders(self) -> list[str]:
        return self.config.get("folders") or replay_folders(default_replay_roots())

    def candidates(self) -> list[str]:
        """Settled monobattle replays saved since tracking began, oldest first."""
        since = self.config["since"]
        prefix = self.config.get("name_prefix", DEFAULT_NAME_PREFIX)
        now = time.time()
        found = []
        for folder in self.folders():
            for path in glob.glob(os.path.join(folder, "*.SC2Replay")):
                if not is_monobattle(path, prefix):
                    continue
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                if stat.st_mtime < since:
                    continue
                settled = self.sizes.get(path) == stat.st_size and now - stat.st_mtime >= SETTLE_SECONDS
                self.sizes[path] = stat.st_size
                if settled:
                    found.append((stat.st_mtime, path))
        return [path for _, path in sorted(found)]

    def scan(self) -> int:
        """Upload what's new; returns how many were sent."""
        sent = 0
        for path in self.candidates():
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                logger.warning("Could not read %s", path)
                continue
            digest = hashlib.sha256(data).hexdigest()
            if digest in self.uploaded:
                continue
            if len(data) > MAX_BYTES:
                logger.warning("Skipping %s: too big for a webhook (%d bytes)", path, len(data))
                self._remember(digest)
                continue
            if self._send(path, data):
                logger.info("Uploaded %s", os.path.basename(path))
                self._remember(digest)
                sent += 1
        return sent

    def _send(self, path: str, data: bytes) -> bool:
        for delay in (*RETRY_DELAYS, None):
            try:
                self.post(self.config["webhook_url"], os.path.basename(path), data, self.config.get("name"))
                return True
            except RateLimited as e:
                time.sleep(e.retry_after)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 404):
                    logger.error("The webhook was refused (%s); ask an admin for a new URL", e.code)
                    return False
                if delay is None:
                    logger.error("Giving up on %s for now (HTTP %s); will retry next start", path, e.code)
                    return False
                time.sleep(delay)
            except (urllib.error.URLError, OSError) as e:
                if delay is None:
                    logger.error("Giving up on %s for now (%s); will retry next start", path, e)
                    return False
                logger.warning("Upload failed (%s); retrying in %ss", e, delay)
                time.sleep(delay)
        return False

    def _remember(self, digest: str) -> None:
        self.uploaded.add(digest)
        self.config["uploaded"] = sorted(self.uploaded)
        save_json(self.config_path, self.config)


class RateLimited(Exception):
    def __init__(self, retry_after: float):
        self.retry_after = retry_after


def post_to_webhook(url: str, file_name: str, data: bytes, username: str | None) -> None:
    payload = {"username": username or "MonoBot Uploader", "allowed_mentions": {"parse": []}}
    boundary = uuid.uuid4().hex
    body = b"".join(
        [
            f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
            f"Content-Type: application/json\r\n\r\n".encode(),
            json.dumps(payload).encode(),
            f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="files[0]"; filename="{file_name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode(),
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    request = urllib.request.Request(
        url + ("&" if "?" in url else "?") + "wait=true",
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": "MonoBotUploader/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60):
            pass
    except urllib.error.HTTPError as e:
        if e.code == 429:
            retry_after = float(json.loads(e.read() or b"{}").get("retry_after", 5))
            raise RateLimited(retry_after) from e
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Upload new monobattle replays to MonoBot.")
    parser.add_argument("--webhook", help="the upload webhook URL from a MonoBot admin (saved for next time)")
    parser.add_argument("--name", help="name shown on your uploads in Discord (saved)")
    parser.add_argument("--folder", action="append", help="a replay folder to watch instead of the defaults (saved)")
    parser.add_argument("--backfill-days", type=float, help="also upload replays from the last N days")
    parser.add_argument("--once", action="store_true", help="scan once and exit")
    parser.add_argument("--config", default=CONFIG_PATH, help=f"settings file (default {CONFIG_PATH})")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    config = load_json(args.config)
    if args.webhook:
        config["webhook_url"] = args.webhook.strip()
    if args.name:
        config["name"] = args.name
    if args.folder:
        config["folders"] = args.folder
    if not config.get("webhook_url"):
        parser.error("no webhook saved yet: run once with --webhook <url>")
    # Only games from here on: the first run doesn't flood the channel with history.
    config.setdefault("since", time.time())
    if args.backfill_days is not None:
        config["since"] = min(config["since"], time.time() - args.backfill_days * 86400)
    save_json(args.config, config)

    uploader = Uploader(config, args.config)
    folders = uploader.folders()
    if not folders:
        logger.error("No SC2 replay folders found; pass --folder <path to Replays/Multiplayer>")
        return 1
    for folder in folders:
        logger.info("Watching %s", folder)
    if args.once:
        # Settling needs two looks at each file.
        uploader.candidates()
        time.sleep(SETTLE_SECONDS)
        uploader.scan()
        return 0
    try:
        while True:
            uploader.scan()
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
