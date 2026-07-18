"""SQLite persistence for parsed matches.

The `matches` + `match_players` tables are the source of truth; ratings are
derived by replaying stored matches through RatingBook (see services.rating),
so parser/rating improvements can always be re-applied to history without
re-parsing replay files.

Schema changes: bump SCHEMA_VERSION, add a numbered function to _MIGRATIONS,
and update _SCHEMA to match (fresh DBs are created at the latest version and
stamped; existing DBs replay only the missing migrations). Never change the
schema by rebuilding the DB — player_links and account_merges are written by
users, not derivable from replays, and a rebuild loses them.

sqlite3 is synchronous — call MatchStore from the bot via asyncio.to_thread
if a call ever shows up in profiling (at monobattle volumes it won't).
"""

import datetime
import hashlib
import json
import logging
import os
import sqlite3
from typing import NamedTuple

from models.replay import MatchPlayer, MonobattleMatch

logger = logging.getLogger(__name__)


class IngestResult(NamedTuple):
    # "inserted" = new game; "updated" = same game, a more complete recording
    # replaced the stored one; "duplicate" = same-or-worse recording, ignored.
    status: str
    match_id: int | None


class LinkResult(NamedTuple):
    # "linked" = claimed (handle bound if the name was found unambiguously in
    # match history); "taken" = another user owns the name; "ambiguous" = the
    # name maps to several accounts in history, so it can't be auto-bound.
    status: str
    owner: str | None = None
    handle: str | None = None
    candidates: int = 0


DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "resources", "monobot.db")

# Stored in each DB file via PRAGMA user_version. Version 1 = the 2026-07
# baseline schema below; pre-versioning DBs read as 0 and are migrated up.
SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY,
    file_hash TEXT NOT NULL UNIQUE,
    content_key TEXT NOT NULL UNIQUE,
    file_name TEXT NOT NULL,
    map_name TEXT NOT NULL,
    played_at TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL,
    game_type TEXT NOT NULL,
    pick_mode TEXT NOT NULL,
    pick_phase_seconds INTEGER NOT NULL,
    winning_team INTEGER,
    winner_confidence REAL NOT NULL,
    winner_method TEXT NOT NULL,
    uploaded_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS match_players (
    match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    toon_handle TEXT NOT NULL DEFAULT '',
    team INTEGER NOT NULL,
    race TEXT NOT NULL,
    pick TEXT,
    repick_used INTEGER,
    unit_counts TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_match_players_match ON match_players(match_id);
CREATE INDEX IF NOT EXISTS idx_match_players_name ON match_players(name);
CREATE INDEX IF NOT EXISTS idx_match_players_handle ON match_players(toon_handle);
CREATE INDEX IF NOT EXISTS idx_matches_played_at ON matches(played_at);

-- Maps a Discord user to the SC2 name(s) they claim. sc2_name is the display
-- name they entered; toon_handle is SC2's unique account id, bound the first
-- time that name is seen in an uploaded game (NULL until then). Rating and
-- participant checks key on the handle, so two players who share a display
-- name never merge.
CREATE TABLE IF NOT EXISTS player_links (
    discord_id TEXT NOT NULL,
    sc2_name TEXT NOT NULL,
    toon_handle TEXT,
    linked_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (sc2_name)
);

CREATE INDEX IF NOT EXISTS idx_player_links_discord ON player_links(discord_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_player_links_handle
    ON player_links(toon_handle) WHERE toon_handle IS NOT NULL;
-- Name claims and lookups are case-insensitive: SC2 names have fixed casing
-- but users won't reproduce it, and "Rain"/"rain" must not be two claims.
CREATE UNIQUE INDEX IF NOT EXISTS idx_player_links_name_nocase
    ON player_links(sc2_name COLLATE NOCASE);

-- Admin-declared account merges: two handles that are the same player even
-- when no Discord link connects them. Combined with link groups (union-find)
-- to build the rating merge map. Stored a<b to dedupe.
CREATE TABLE IF NOT EXISTS account_merges (
    handle_a TEXT NOT NULL,
    handle_b TEXT NOT NULL,
    merged_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (handle_a, handle_b)
);

-- Channels watched for replay uploads, managed at runtime by !watchreplays.
-- Unioned with the config-file channel list; empty overall = watch everywhere.
CREATE TABLE IF NOT EXISTS replay_channels (
    channel_id INTEGER PRIMARY KEY,
    guild_id INTEGER,
    added_by TEXT,
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def hash_replay(data: bytes) -> str:
    """Content hash used to deduplicate uploads of the same replay file."""
    return hashlib.sha256(data).hexdigest()


def content_key(match: MonobattleMatch) -> str:
    """Identity of the actual game, invariant across who recorded it. Two
    players' recordings of the same game differ in bytes and length (they may
    leave at different times) but share the start time and full roster.
    Minute precision absorbs sub-second clock differences between clients."""
    return _content_key_raw(match.played_at, [p.name for p in match.players])


def _content_key_raw(played_at: datetime.datetime, names: list[str]) -> str:
    minute = played_at.replace(second=0, microsecond=0)
    return hashlib.sha256(f"{minute.isoformat()}|{'|'.join(sorted(names))}".encode()).hexdigest()


def _migration_1_content_key(conn: sqlite3.Connection) -> None:
    """Backfill content_key on DBs created before it existed. No-op on DBs
    that already have the column (the pre-versioning baseline)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(matches)")}
    if "content_key" not in cols:
        conn.execute("ALTER TABLE matches ADD COLUMN content_key TEXT")
        for row in conn.execute("SELECT id, played_at FROM matches").fetchall():
            names = [r["name"] for r in conn.execute("SELECT name FROM match_players WHERE match_id = ?", (row["id"],))]
            key = _content_key_raw(datetime.datetime.fromisoformat(row["played_at"]), names)
            conn.execute("UPDATE matches SET content_key = ? WHERE id = ?", (key, row["id"]))
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_matches_content_key ON matches(content_key)")


def _migration_2_replay_channels(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS replay_channels (
               channel_id INTEGER PRIMARY KEY,
               guild_id INTEGER,
               added_by TEXT,
               added_at TEXT NOT NULL DEFAULT (datetime('now'))
           )"""
    )


# version -> function upgrading a DB from version-1 to version. Each runs in
# its own transaction and must leave the DB identical to a fresh _SCHEMA
# creation at that version.
_MIGRATIONS = {
    1: _migration_1_content_key,
    2: _migration_2_replay_channels,
}


class MatchStore:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        fresh = self._conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'matches'").fetchone()
        self._conn.executescript(_SCHEMA)
        if fresh is None:
            # Brand-new DB: _SCHEMA created it at the latest version already.
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.commit()
        else:
            self._migrate()
        # Bumped on every write so callers (e.g. the leaderboard cog) can
        # cache derived data like rating books and know when it's stale.
        self.change_count = 0

    def _migrate(self) -> None:
        """Bring an existing DB up to SCHEMA_VERSION, one step at a time."""
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"{self._db_path} is at schema version {version}, newer than this code ({SCHEMA_VERSION}) — "
                "refusing to open it (running old code against a migrated DB corrupts it)."
            )
        for target in range(version + 1, SCHEMA_VERSION + 1):
            logger.info("Migrating %s to schema version %d", self._db_path, target)
            _MIGRATIONS[target](self._conn)
            self._conn.execute(f"PRAGMA user_version = {target}")
            self._conn.commit()

    def close(self):
        self._conn.close()

    # -- writes ----------------------------------------------------------

    def ingest(self, match: MonobattleMatch, file_hash: str, uploaded_by: str | None = None) -> IngestResult:
        """Store a parsed match. If the same game (by content_key) is already
        stored, keep whichever recording is more complete — a higher winner
        confidence, or a longer duration at equal confidence — so a teammate
        who stayed to the end can supply a winner the early-leaver's replay
        lacked. Manually confirmed results are never overwritten."""
        key = content_key(match)
        existing = self._conn.execute(
            "SELECT id, file_hash, duration_seconds, winner_confidence, winner_method FROM matches WHERE content_key = ?",
            (key,),
        ).fetchone()

        if existing is not None:
            if existing["file_hash"] == file_hash:
                return IngestResult("duplicate", existing["id"])
            if not self._is_more_complete(match, existing):
                logger.info("Same game already stored, keeping better recording: %s", match.file_name)
                return IngestResult("duplicate", existing["id"])
            self._replace_match(existing["id"], match, file_hash, key, uploaded_by)
            self.bind_handles_from_match(match)
            self.change_count += 1
            return IngestResult("updated", existing["id"])

        try:
            cur = self._conn.execute(
                """INSERT INTO matches
                   (file_hash, content_key, file_name, map_name, played_at, duration_seconds,
                    game_type, pick_mode, pick_phase_seconds, winning_team,
                    winner_confidence, winner_method, uploaded_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (file_hash, key, *self._match_columns(match), uploaded_by),
            )
        except sqlite3.IntegrityError:
            # Exact file already stored under a different game (shouldn't happen).
            logger.info("Duplicate replay ignored: %s", match.file_name)
            return IngestResult("duplicate", None)
        match_id = cur.lastrowid
        self._insert_players(match_id, match)
        self._conn.commit()
        self.bind_handles_from_match(match)
        self.change_count += 1
        return IngestResult("inserted", match_id)

    @staticmethod
    def _is_more_complete(match: MonobattleMatch, existing: sqlite3.Row) -> bool:
        if existing["winner_method"] == "confirmed":
            return False  # manual confirmation is authoritative
        return (match.winner_confidence, match.duration_seconds) > (
            existing["winner_confidence"],
            existing["duration_seconds"],
        )

    @staticmethod
    def _match_columns(match: MonobattleMatch) -> tuple:
        return (
            match.file_name,
            match.map_name,
            match.played_at.isoformat(),
            match.duration_seconds,
            match.game_type,
            match.pick_mode,
            match.pick_phase_seconds,
            match.winning_team,
            match.winner_confidence,
            match.winner_method,
        )

    def _insert_players(self, match_id: int, match: MonobattleMatch) -> None:
        self._conn.executemany(
            """INSERT INTO match_players
               (match_id, name, toon_handle, team, race, pick, repick_used, unit_counts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    match_id,
                    p.name,
                    p.toon_handle,
                    p.team,
                    p.race,
                    p.pick,
                    None if p.repick_used is None else int(p.repick_used),
                    json.dumps(p.unit_counts),
                )
                for p in match.players
            ],
        )

    def _replace_match(
        self, match_id: int, match: MonobattleMatch, file_hash: str, key: str, uploaded_by: str | None
    ) -> None:
        self._conn.execute(
            """UPDATE matches SET
                 file_hash = ?, content_key = ?, file_name = ?, map_name = ?, played_at = ?,
                 duration_seconds = ?, game_type = ?, pick_mode = ?, pick_phase_seconds = ?,
                 winning_team = ?, winner_confidence = ?, winner_method = ?, uploaded_by = ?
               WHERE id = ?""",
            (file_hash, key, *self._match_columns(match), uploaded_by, match_id),
        )
        self._conn.execute("DELETE FROM match_players WHERE match_id = ?", (match_id,))
        self._insert_players(match_id, match)
        self._conn.commit()

    def link_player(self, discord_id: str, sc2_name: str) -> LinkResult:
        """Claim an SC2 display name and bind its account handle if the name
        maps unambiguously to one account in match history. Names with no
        history bind later, the first time they're seen in an uploaded game."""
        owner = self.discord_id_for(sc2_name)
        if owner is not None and owner != discord_id:
            return LinkResult("taken", owner=owner)
        if owner is None:
            self._conn.execute(
                "INSERT INTO player_links (discord_id, sc2_name) VALUES (?, ?)",
                (discord_id, sc2_name),
            )
            self.change_count += 1
        handles = self.handles_for_name(sc2_name)
        if len(handles) == 1:
            self._try_bind(sc2_name)
        self._conn.commit()
        if len(handles) > 1:
            return LinkResult("ambiguous", candidates=len(handles))
        return LinkResult("linked", handle=handles[0] if handles else None)

    def bind_handles_from_match(self, match: MonobattleMatch) -> None:
        """After storing a game, bind handles for any claimed names it reveals
        (used when a name was linked before it had any match history)."""
        for name in {p.name for p in match.players}:
            self._try_bind(name)
        self._conn.commit()

    def handles_for_name(self, sc2_name: str) -> list[str]:
        """Every account that has played under a display name — current OR
        former, since match_players keeps each game's name. Case-insensitive."""
        rows = self._conn.execute(
            "SELECT DISTINCT toon_handle FROM match_players WHERE name = ? COLLATE NOCASE AND toon_handle != ''",
            (sc2_name,),
        ).fetchall()
        return [r["toon_handle"] for r in rows]

    def candidates_for_name(self, sc2_name: str) -> list[tuple[str, str, int]]:
        """Accounts that have played under a name — (handle, current name,
        game count), most-active first. Used to disambiguate a shared name."""
        rows = self._conn.execute(
            """SELECT toon_handle AS h, COUNT(*) AS games
               FROM match_players WHERE name = ? COLLATE NOCASE AND toon_handle != ''
               GROUP BY toon_handle ORDER BY games DESC""",
            (sc2_name,),
        ).fetchall()
        out = []
        for r in rows:
            aliases = self.aliases_for_handle(r["h"])
            out.append((r["h"], aliases[0] if aliases else r["h"], r["games"]))
        return out

    def bind_specific(self, discord_id: str, sc2_name: str, handle: str) -> bool:
        """Claim a name and bind it to a specific chosen account (resolves an
        ambiguous link). False if the name or account is already taken."""
        owner = self.discord_id_for(sc2_name)
        if owner is not None and owner != discord_id:
            return False
        if owner is None:
            self._conn.execute("INSERT INTO player_links (discord_id, sc2_name) VALUES (?, ?)", (discord_id, sc2_name))
        try:
            self._conn.execute(
                "UPDATE player_links SET toon_handle = ? WHERE sc2_name = ? COLLATE NOCASE", (handle, sc2_name)
            )
        except sqlite3.IntegrityError:
            self._conn.commit()
            return False  # that account is already bound to someone else
        self._conn.commit()
        self.change_count += 1
        return True

    def add_account(self, discord_id: str, handle: str) -> bool:
        """Bind an ADDITIONAL account (by handle) to a user, e.g. a cross-region
        copy that shares a display name with one they've already linked. Uses
        the handle as a placeholder claim name to avoid the one-row-per-name
        constraint. False if the account is already linked to someone else."""
        rows = self._conn.execute("SELECT discord_id FROM player_links WHERE toon_handle = ?", (handle,)).fetchall()
        if rows:
            return all(r["discord_id"] == discord_id for r in rows)
        try:
            self._conn.execute(
                "INSERT INTO player_links (discord_id, sc2_name, toon_handle) VALUES (?, ?, ?)",
                (discord_id, handle, handle),
            )
        except sqlite3.IntegrityError:
            return False
        self._conn.commit()
        self.change_count += 1
        return True

    def aliases_for_handle(self, toon_handle: str) -> list[str]:
        """Every display name an account has played under, most recent first."""
        rows = self._conn.execute(
            """SELECT mp.name AS name, MAX(m.played_at) AS last
               FROM match_players mp JOIN matches m ON m.id = mp.match_id
               WHERE mp.toon_handle = ?
               GROUP BY mp.name COLLATE NOCASE
               ORDER BY last DESC""",
            (toon_handle,),
        ).fetchall()
        return [r["name"] for r in rows]

    def _try_bind(self, sc2_name: str) -> None:
        """Bind an unbound link for sc2_name iff the name maps to exactly one
        account across all stored matches (else it's ambiguous — leave it)."""
        handles = self.handles_for_name(sc2_name)
        if len(handles) != 1:
            return
        try:
            self._conn.execute(
                "UPDATE player_links SET toon_handle = ? WHERE sc2_name = ? COLLATE NOCASE AND toon_handle IS NULL",
                (handles[0], sc2_name),
            )
        except sqlite3.IntegrityError:
            pass  # handle already bound to another claimed name; leave as-is

    def unlink_player(self, discord_id: str, sc2_name: str) -> bool:
        """Release a name the user owns; returns False if they didn't own it."""
        cur = self._conn.execute(
            "DELETE FROM player_links WHERE sc2_name = ? COLLATE NOCASE AND discord_id = ?",
            (sc2_name, discord_id),
        )
        self._conn.commit()
        if cur.rowcount:
            self.change_count += 1
            return True
        return False

    def toggle_replay_channel(self, channel_id: int, guild_id: int | None, added_by: str) -> bool:
        """Watch a channel for replay uploads, or stop if it's already
        watched. Returns True if the channel is now watched."""
        cur = self._conn.execute("DELETE FROM replay_channels WHERE channel_id = ?", (channel_id,))
        if cur.rowcount == 0:
            self._conn.execute(
                "INSERT INTO replay_channels (channel_id, guild_id, added_by) VALUES (?, ?, ?)",
                (channel_id, guild_id, added_by),
            )
        self._conn.commit()
        return cur.rowcount == 0

    def replay_channel_ids(self) -> set[int]:
        return {r["channel_id"] for r in self._conn.execute("SELECT channel_id FROM replay_channels")}

    def confirm_winner(self, match_id: int, winning_team: int) -> None:
        """Manual confirmation overrides any inferred result."""
        self._conn.execute(
            "UPDATE matches SET winning_team = ?, winner_confidence = 1.0, winner_method = 'confirmed' WHERE id = ?",
            (winning_team, match_id),
        )
        self._conn.commit()
        self.change_count += 1

    # -- reads -----------------------------------------------------------

    def sc2_names_for(self, discord_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT sc2_name FROM player_links WHERE discord_id = ? ORDER BY sc2_name", (discord_id,)
        ).fetchall()
        return [r["sc2_name"] for r in rows]

    def handles_for(self, discord_id: str) -> list[str]:
        """The SC2 account handles bound to this user (names they've played
        under at least once). Empty if linked-but-never-played."""
        rows = self._conn.execute(
            "SELECT toon_handle FROM player_links WHERE discord_id = ? AND toon_handle IS NOT NULL ORDER BY toon_handle",
            (discord_id,),
        ).fetchall()
        return [r["toon_handle"] for r in rows]

    def merge_accounts(self, handle_a: str, handle_b: str) -> None:
        """Declare two accounts the same player (admin merge)."""
        a, b = sorted((handle_a, handle_b))
        self._conn.execute("INSERT OR IGNORE INTO account_merges (handle_a, handle_b) VALUES (?, ?)", (a, b))
        self._conn.commit()
        self.change_count += 1

    def unmerge_accounts(self, handle_a: str, handle_b: str) -> bool:
        a, b = sorted((handle_a, handle_b))
        cur = self._conn.execute("DELETE FROM account_merges WHERE handle_a = ? AND handle_b = ?", (a, b))
        self._conn.commit()
        if cur.rowcount:
            self.change_count += 1
        return cur.rowcount > 0

    def merge_map(self) -> dict[str, str]:
        """A person's multiple accounts collapse to one canonical handle so
        they share a single rating. Combines two sources via union-find:
        handles linked to the same Discord user, and admin-declared merges.
        Solo accounts are omitted (they map to themselves)."""
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            parent.setdefault(x, x)
            root = x
            while parent[root] != root:
                root = parent[root]
            while parent[x] != root:  # path compression
                parent[x], x = root, parent[x]
            return root

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)  # min handle stays canonical

        groups: dict[str, list[str]] = {}
        for r in self._conn.execute("SELECT discord_id, toon_handle FROM player_links WHERE toon_handle IS NOT NULL"):
            groups.setdefault(r["discord_id"], []).append(r["toon_handle"])
        for handles in groups.values():
            for h in handles[1:]:
                union(handles[0], h)
        for r in self._conn.execute("SELECT handle_a, handle_b FROM account_merges"):
            union(r["handle_a"], r["handle_b"])

        members: dict[str, list[str]] = {}
        for h in list(parent):
            members.setdefault(find(h), []).append(h)
        mapping: dict[str, str] = {}
        for root, hs in members.items():
            if len(hs) > 1:
                for h in hs:
                    mapping[h] = root
        return mapping

    def merged_handles(self, handle: str) -> list[str]:
        """Every handle in this handle's account-merge group (itself included).
        Stats queries must cover the whole group, not just the canonical
        handle, since match_players rows keep each game's real account."""
        merge_map = self.merge_map()
        root = merge_map.get(handle, handle)
        group = sorted(h for h, r in merge_map.items() if r == root)
        return group or [handle]

    def aliases_for_handles(self, handles: list[str]) -> list[str]:
        """Display names across a group of accounts, most recent first."""
        if not handles:
            return []
        placeholders = ",".join("?" * len(handles))
        rows = self._conn.execute(
            f"""SELECT mp.name AS name, MAX(m.played_at) AS last
               FROM match_players mp JOIN matches m ON m.id = mp.match_id
               WHERE mp.toon_handle IN ({placeholders})
               GROUP BY mp.name COLLATE NOCASE
               ORDER BY last DESC""",
            handles,
        ).fetchall()
        return [r["name"] for r in rows]

    def pending_names_for(self, discord_id: str) -> list[str]:
        """Names this user has claimed that aren't bound to an account yet
        (never seen in a game, or ambiguous)."""
        rows = self._conn.execute(
            "SELECT sc2_name FROM player_links WHERE discord_id = ? AND toon_handle IS NULL ORDER BY sc2_name",
            (discord_id,),
        ).fetchall()
        return [r["sc2_name"] for r in rows]

    def discord_id_for(self, sc2_name: str) -> str | None:
        row = self._conn.execute(
            "SELECT discord_id FROM player_links WHERE sc2_name = ? COLLATE NOCASE", (sc2_name,)
        ).fetchone()
        return row["discord_id"] if row else None

    def discord_id_for_handle(self, toon_handle: str) -> str | None:
        row = self._conn.execute("SELECT discord_id FROM player_links WHERE toon_handle = ?", (toon_handle,)).fetchone()
        return row["discord_id"] if row else None

    def has_replay(self, file_hash: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM matches WHERE file_hash = ?", (file_hash,)).fetchone()
        return row is not None

    def get_match(self, match_id: int) -> MonobattleMatch | None:
        row = self._conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
        if row is None:
            return None
        return self._build_match(row)

    def all_matches(self) -> list[tuple[int, MonobattleMatch]]:
        """All matches with their ids, oldest first (rating order)."""
        rows = self._conn.execute("SELECT * FROM matches ORDER BY played_at").fetchall()
        return [(row["id"], self._build_match(row)) for row in rows]

    def pending_confirmations(self, confidence_gate: float, min_duration: int) -> list[tuple[int, MonobattleMatch]]:
        """Real matches whose winner is unknown or below the rating gate."""
        rows = self._conn.execute(
            """SELECT * FROM matches
               WHERE (winning_team IS NULL OR winner_confidence < ?)
                 AND duration_seconds >= ?
               ORDER BY played_at""",
            (confidence_gate, min_duration),
        ).fetchall()
        return [(row["id"], self._build_match(row)) for row in rows]

    def match_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]

    def player_records_by(
        self, handles: str | list[str], column: str, confidence_gate: float, min_duration: int
    ) -> dict[str, list[int]]:
        """Wins/losses grouped by 'race' or 'pick' over decided matches, for
        one account or a merged group of accounts. Returns {value: [wins, losses]}."""
        if column not in ("race", "pick"):
            raise ValueError(f"unsupported column: {column}")
        if isinstance(handles, str):
            handles = [handles]
        placeholders = ",".join("?" * len(handles))
        rows = self._conn.execute(
            f"""SELECT mp.{column} AS val, (mp.team = m.winning_team) AS won, COUNT(*) AS n
               FROM match_players mp JOIN matches m ON m.id = mp.match_id
               WHERE mp.toon_handle IN ({placeholders})
                 AND m.winning_team IS NOT NULL
                 AND m.winner_confidence >= ?
                 AND m.duration_seconds >= ?
                 AND mp.{column} IS NOT NULL
               GROUP BY mp.{column}, won""",
            (*handles, confidence_gate, min_duration),
        ).fetchall()
        records: dict[str, list[int]] = {}
        for r in rows:
            entry = records.setdefault(r["val"], [0, 0])
            entry[0 if r["won"] else 1] = r["n"]
        return records

    def known_player(self, sc2_name: str) -> bool:
        """Whether this SC2 name appears in any stored match (typo guard)."""
        row = self._conn.execute(
            "SELECT 1 FROM match_players WHERE name = ? COLLATE NOCASE LIMIT 1", (sc2_name,)
        ).fetchone()
        return row is not None

    def unit_records(self, confidence_gate: float, min_duration: int) -> dict[str, list[int]]:
        """pick -> [wins, losses] across decided matches."""
        rows = self._conn.execute(
            """SELECT mp.pick AS pick, (mp.team = m.winning_team) AS won, COUNT(*) AS n
               FROM match_players mp JOIN matches m ON m.id = mp.match_id
               WHERE m.winning_team IS NOT NULL
                 AND m.winner_confidence >= ?
                 AND m.duration_seconds >= ?
                 AND mp.pick IS NOT NULL
               GROUP BY mp.pick, won""",
            (confidence_gate, min_duration),
        ).fetchall()
        records: dict[str, list[int]] = {}
        for row in rows:
            entry = records.setdefault(row["pick"], [0, 0])
            entry[0 if row["won"] else 1] = row["n"]
        return records

    # -- internals -------------------------------------------------------

    def _build_match(self, row: sqlite3.Row) -> MonobattleMatch:
        player_rows = self._conn.execute(
            "SELECT * FROM match_players WHERE match_id = ? ORDER BY team, name", (row["id"],)
        ).fetchall()
        players = [
            MatchPlayer(
                name=p["name"],
                toon_handle=p["toon_handle"],
                team=p["team"],
                race=p["race"],
                pick=p["pick"],
                repick_used=None if p["repick_used"] is None else bool(p["repick_used"]),
                unit_counts=json.loads(p["unit_counts"]),
            )
            for p in player_rows
        ]
        return MonobattleMatch(
            file_name=row["file_name"],
            map_name=row["map_name"],
            played_at=datetime.datetime.fromisoformat(row["played_at"]),
            duration_seconds=row["duration_seconds"],
            game_type=row["game_type"],
            pick_mode=row["pick_mode"],
            pick_phase_seconds=row["pick_phase_seconds"],
            players=players,
            winning_team=row["winning_team"],
            winner_confidence=row["winner_confidence"],
            winner_method=row["winner_method"],
        )
