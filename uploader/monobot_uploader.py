"""Watch the SC2 replay folders and send new replays to MonoBot's upload API,
which keeps only the monobattles. Standard library only, so it runs on any
Python 3.10+.

    python monobot_uploader.py --server https://... --token ...   # first run saves both
    python monobot_uploader.py                                    # then just this

Get the command (token included) from the bot with /uploadtoken. The bot
decides which server a game belongs to; this only finds and sends files.
See README.md."""

import argparse
import glob
import hashlib
import json
import logging
import logging.handlers
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger("monobot_uploader")

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".monobot_uploader.json")
LOG_PATH = os.path.join(os.path.expanduser("~"), ".monobot_uploader.log")
POLL_SECONDS = 10
# SC2 writes the file at game end; wait until it has stopped changing.
SETTLE_SECONDS = 5
# The server refuses bigger (real replays are 0.1-2 MB).
MAX_BYTES = 10 * 1024 * 1024
RETRY_DELAYS = (5, 30, 120)
USER_AGENT = "MonoBotUploader/2"


class BadToken(Exception):
    """The server doesn't know this token: no point retrying."""


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


def _request(server: str, token: str, path: str, data: bytes | None = None, headers: dict | None = None) -> dict:
    request = urllib.request.Request(
        server.rstrip("/") + path,
        data=data,
        method="POST" if data is not None else "GET",
        headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT, **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise BadToken() from e
        raise


def check_token(server: str, token: str) -> str:
    """The Discord name the token belongs to."""
    return _request(server, token, "/v1/me")["user"]


def post_replay(server: str, token: str, file_name: str, data: bytes) -> dict:
    return _request(
        server,
        token,
        "/v1/replays",
        data,
        {"Content-Type": "application/octet-stream", "X-Replay-Name": urllib.parse.quote(file_name, safe=" ()")},
    )


def describe(result: dict) -> str:
    status = result.get("status")
    if status == "stored":
        return f"stored on {result.get('server') or 'the ladder'} as match #{result.get('match')}"
    if status == "duplicate":
        return f"already on {result.get('server') or 'the ladder'}"
    if status == "rejected":
        return f"skipped ({result.get('reason')})"
    return f"the bot couldn't read it ({result.get('reason') or status})"


class Uploader:
    def __init__(self, config: dict, config_path: str = CONFIG_PATH, post=None):
        self.config = config
        self.config_path = config_path
        self.post = post or post_replay
        self.uploaded: set[str] = set(config.get("uploaded", []))
        self.sizes: dict[str, int] = {}  # path -> size at the last scan

    def folders(self) -> list[str]:
        return self.config.get("folders") or replay_folders(default_replay_roots())

    def candidates(self) -> list[str]:
        """Settled replays saved since tracking began, oldest first. Every one:
        the file name is the map's, and a MonoBattle mod game is named after
        whichever melee map it was played on, so only the server can tell."""
        since = self.config["since"]
        now = time.time()
        found = []
        for folder in self.folders():
            for path in glob.glob(os.path.join(folder, "*.SC2Replay")):
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
        """Send what's new; returns how many the server answered. Raises
        BadToken if the server refuses the token."""
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
                logger.warning("Skipping %s: too big (%d bytes)", path, len(data))
                self._remember(digest)
                continue
            result = self._send(path, data)
            if result is not None:
                logger.info("%s: %s", os.path.basename(path), describe(result))
                self._remember(digest)
                sent += 1
        return sent

    def _send(self, path: str, data: bytes) -> dict | None:
        for delay in (*RETRY_DELAYS, None):
            try:
                return self.post(self.config["server_url"], self.config["token"], os.path.basename(path), data)
            except BadToken:
                raise
            except urllib.error.HTTPError as e:
                if 400 <= e.code < 500:
                    logger.error("The server refused %s (HTTP %s)", path, e.code)
                    return None
                problem = f"HTTP {e.code}"
            except (urllib.error.URLError, OSError, ValueError) as e:
                problem = str(e)
            if delay is None:
                logger.error("Giving up on %s for now (%s); it'll be retried next start", path, problem)
                return None
            logger.warning("Upload failed (%s); retrying in %ss", problem, delay)
            time.sleep(delay)
        return None

    def _remember(self, digest: str) -> None:
        self.uploaded.add(digest)
        self.config["uploaded"] = sorted(self.uploaded)
        save_json(self.config_path, self.config)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Upload new monobattle replays to MonoBot.")
    parser.add_argument("--server", help="the upload server address (saved for next time)")
    parser.add_argument("--token", help="your token from /uploadtoken (saved for next time)")
    parser.add_argument("--folder", action="append", help="a replay folder to watch instead of the defaults (saved)")
    parser.add_argument("--backfill-days", type=float, help="also upload replays from the last N days")
    parser.add_argument("--once", action="store_true", help="scan once and exit")
    parser.add_argument("--config", default=CONFIG_PATH, help=f"settings file (default {CONFIG_PATH})")
    args = parser.parse_args(argv)
    # Also a file, since pythonw (run at login, no window) has no console.
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    ]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S", handlers=handlers
    )

    config = load_json(args.config)
    if args.server:
        config["server_url"] = args.server.strip()
    if args.token:
        config["token"] = args.token.strip()
    if args.folder:
        config["folders"] = args.folder
    if not (config.get("server_url") and config.get("token")):
        parser.error("no server/token saved yet: run the command /uploadtoken gives you")
    # Only games from here on: the first run doesn't send years of history.
    config.setdefault("since", time.time())
    if args.backfill_days is not None:
        config["since"] = min(config["since"], time.time() - args.backfill_days * 86400)
    save_json(args.config, config)

    try:
        logger.info("Uploading as %s", check_token(config["server_url"], config["token"]))
    except BadToken:
        logger.error("The server doesn't know this token. Get a new one with /uploadtoken.")
        return 1
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        logger.warning("Can't reach %s right now (%s); will keep trying", config["server_url"], e)

    uploader = Uploader(config, args.config)
    folders = uploader.folders()
    if not folders:
        logger.error("No SC2 replay folders found; pass --folder <path to Replays/Multiplayer>")
        return 1
    for folder in folders:
        logger.info("Watching %s", folder)
    try:
        if args.once:
            # Settling needs two looks at each file.
            uploader.candidates()
            time.sleep(SETTLE_SECONDS)
            uploader.scan()
            return 0
        while True:
            uploader.scan()
            time.sleep(POLL_SECONDS)
    except BadToken:
        logger.error("The server stopped accepting this token. Get a new one with /uploadtoken.")
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
