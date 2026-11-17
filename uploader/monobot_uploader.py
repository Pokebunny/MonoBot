"""Watch the SC2 replay folders and send new monobattle replays to MonoBot's
upload API: anything on the arcade map, and any 4v4 off a Blizzard map (it
may be a MonoBattle mod game, which only the server can confirm). Standard
library only, so it runs on any Python 3.10+.

    python monobot_uploader.py --server https://... --token ...   # first run saves both
    python monobot_uploader.py                                    # then just this

Get the command (token included) from the bot with /uploadtoken. The bot
decides which server a game belongs to; this only finds and sends files.
See README.md."""

import argparse
import bz2
import glob
import hashlib
import json
import logging
import logging.handlers
import os
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import NamedTuple

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


# -- a peek at the replay, so obvious non-monobattles are never sent ---------
# A replay is an MPQ archive; its replay.details file holds the map title and
# the players. Reading just that takes the standard library and milliseconds
# (the server's full parse takes seconds). Layout as in Blizzard's s2protocol
# and the mpyq library.
ARCADE_MAP_PREFIX = "Monobattle"  # every game on the old arcade map


def _crypt_table() -> list[int]:
    table = [0] * 0x500
    seed = 0x00100001
    for i in range(0x100):
        index = i
        for _ in range(5):
            seed = (seed * 125 + 3) % 0x2AAAAB
            high = (seed & 0xFFFF) << 16
            seed = (seed * 125 + 3) % 0x2AAAAB
            table[index] = high | (seed & 0xFFFF)
            index += 0x100
    return table


_CRYPT = _crypt_table()
_MASK = 0xFFFFFFFF


def _mpq_hash(name: str, kind: int) -> int:
    """kind: 1 and 2 are a file's two name hashes, 3 a table's key."""
    seed1, seed2 = 0x7FED7FED, 0xEEEEEEEE
    for ch in name.upper().encode():
        seed1 = _CRYPT[(kind << 8) + ch] ^ ((seed1 + seed2) & _MASK)
        seed2 = (ch + seed1 + seed2 + (seed2 << 5) + 3) & _MASK
    return seed1


def _decrypt(data: bytes, key: int) -> bytes:
    seed1, seed2 = key, 0xEEEEEEEE
    out = []
    for (value,) in struct.iter_unpack("<I", data):
        seed2 = (seed2 + _CRYPT[0x400 + (seed1 & 0xFF)]) & _MASK
        value ^= (seed1 + seed2) & _MASK
        seed1 = (((~seed1 << 21) + 0x11111111) | (seed1 >> 11)) & _MASK
        seed2 = (value + seed2 + (seed2 << 5) + 3) & _MASK
        out.append(value)
    return struct.pack(f"<{len(out)}I", *out)


def _decompress(data: bytes) -> bytes:
    kind, body = data[0], data[1:]
    if kind == 0x10:
        return bz2.decompress(body)
    if kind == 0x02:
        return zlib.decompress(body)
    if kind == 0:
        return body
    raise ValueError(f"unknown MPQ compression {kind:#x}")


def _mpq_file(archive: bytes, name: str) -> bytes:
    """One file out of an SC2 replay (an MPQ behind a user-data header)."""
    if archive[:4] != b"MPQ\x1b":
        raise ValueError("not an SC2 replay")
    base = struct.unpack_from("<I", archive, 8)[0]  # the archive proper starts here
    if archive[base : base + 4] != b"MPQ\x1a":
        raise ValueError("no MPQ header")
    sector_shift, hash_offset, block_offset, hash_count, block_count = struct.unpack_from("<HIIII", archive, base + 14)
    hash_start, block_start = base + hash_offset, base + block_offset
    hashes = _decrypt(archive[hash_start : hash_start + hash_count * 16], _mpq_hash("(hash table)", 3))
    blocks = _decrypt(archive[block_start : block_start + block_count * 16], _mpq_hash("(block table)", 3))
    want = (_mpq_hash(name, 1), _mpq_hash(name, 2))
    for hash_a, hash_b, _locale, _platform, block_index in struct.iter_unpack("<IIHHI", hashes):
        if (hash_a, hash_b) == want and block_index < block_count:
            break
    else:
        raise ValueError(f"{name} not in replay")
    offset, packed_size, size, flags = struct.unpack_from("<IIII", blocks, block_index * 16)
    data = archive[base + offset : base + offset + packed_size]
    compressed = flags & 0x00000200
    if flags & 0x01000000:  # stored as one unit
        return _decompress(data) if compressed and size > packed_size else data
    sector_size = 512 << sector_shift
    sectors = (size + sector_size - 1) // sector_size
    has_crc = bool(flags & 0x04000000)
    bounds = struct.unpack_from(f"<{sectors + 1 + has_crc}I", data)
    out, left = bytearray(), size
    for start, end in zip(bounds[:sectors], bounds[1 : sectors + 1]):
        sector = data[start:end]
        if compressed and left > len(sector):
            sector = _decompress(sector)
        left -= len(sector)
        out += sector
    return bytes(out)


class _Versioned:
    """Blizzard's versioned encoding: every value carries a type tag, so it
    decodes without the protocol's type tables. Structs come back as
    {field tag: value}."""

    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def _byte(self) -> int:
        self.pos += 1
        return self.data[self.pos - 1]

    def _vint(self) -> int:
        b = self._byte()
        negative, value, shift = b & 1, (b >> 1) & 0x3F, 6
        while b & 0x80:
            b = self._byte()
            value |= (b & 0x7F) << shift
            shift += 7
        return -value if negative else value

    def _take(self, n: int) -> bytes:
        self.pos += n
        return self.data[self.pos - n : self.pos]

    def value(self):
        tag = self._byte()
        if tag == 0:  # array
            return [self.value() for _ in range(self._vint())]
        if tag == 1:  # bit array
            return self._take((self._vint() + 7) // 8)
        if tag == 2:  # blob
            return self._take(self._vint())
        if tag == 3:  # choice
            self._vint()
            return self.value()
        if tag == 4:  # optional
            return self.value() if self._byte() else None
        if tag == 5:  # struct
            return {self._vint(): self.value() for _ in range(self._vint())}
        if tag in (6, 7, 8):  # u8 / u32 / u64
            return int.from_bytes(self._take({6: 1, 7: 4, 8: 8}[tag]), "big")
        if tag == 9:
            return self._vint()
        raise ValueError(f"unknown versioned tag {tag}")


class Peek(NamedTuple):
    title: str
    teams: list[int]  # players per team, largest first
    blizzard_map: bool


def peek(data: bytes) -> Peek:
    """What replay.details says. Field numbers are s2protocol's: 0 players,
    1 title, 4 m_isBlizzardMap; a player's 5 team, 7 observer."""
    details = _Versioned(_mpq_file(data, "replay.details")).value()
    teams: dict[int, int] = {}
    for player in details.get(0) or []:
        if not player.get(7):
            teams[player.get(5)] = teams.get(player.get(5), 0) + 1
    return Peek(details[1].decode("utf-8", "replace"), sorted(teams.values(), reverse=True), bool(details.get(4)))


def worth_sending(data: bytes) -> tuple[bool, str]:
    """Whether the server could keep this game, and why not.

    The MonoBattle mod can't be seen here (it's one dependency hash among
    eight, new with every publish), but it clears m_isBlizzardMap even on a
    Blizzard melee map: all 4 mod test games did, while all 361 of the
    owner's 4v4s flagged as Blizzard maps were co-op or ladder. So a 4v4
    off a Blizzard map is sent and the server's parse decides. A replay this
    can't read is sent too."""
    try:
        title, teams, blizzard_map = peek(data)
    except Exception as e:  # an odd file costs the server a parse, never a game
        logger.debug("Couldn't peek at a replay (%s); sending it", e)
        return True, ""
    if title.lower().startswith(ARCADE_MAP_PREFIX.lower()):
        return True, ""
    if teams == [4, 4]:
        return (False, f"4v4 on {title}, a Blizzard map without the mod") if blizzard_map else (True, "")
    return False, f"{'v'.join(map(str, teams)) or 'no players'} on {title}"


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
        """Settled replays saved since tracking began, oldest first, whatever
        their name: a MonoBattle mod game is named after its melee map.
        worth_sending then looks inside."""
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
            send, why = worth_sending(data)
            if not send:
                logger.info("%s: skipped, not a monobattle (%s)", os.path.basename(path), why)
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
