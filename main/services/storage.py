"""SQLite persistence for parsed matches.

The `matches` + `match_players` tables are the source of truth; ratings are
derived by replaying stored matches through RatingBook (see services.rating),
so parser/rating improvements can always be re-applied to history without
re-parsing replay files.

Schema changes: bump SCHEMA_VERSION, add a numbered function to _MIGRATIONS,
and update _SCHEMA to match (fresh DBs are created at the latest version and
stamped; existing DBs replay only the missing migrations). Never change the
schema by rebuilding the DB — player_links rows are written by users, not
derivable from replays, and a rebuild loses them.

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
SCHEMA_VERSION = 8

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
    comeback_deficit INTEGER,
    lead_changes INTEGER,
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
    repick_from TEXT,
    resources_killed INTEGER,
    econ_killed INTEGER,
    tech_killed INTEGER,
    resources_lost INTEGER,
    resources_floated INTEGER,
    drop_commands INTEGER,
    static_defense INTEGER,
    bases_before_unit INTEGER,
    orbitals INTEGER,
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

-- Channels watched for replay uploads, managed at runtime by !watchreplays.
-- Unioned with the config-file channel list; empty overall = watch everywhere.
CREATE TABLE IF NOT EXISTS replay_channels (
    channel_id INTEGER PRIMARY KEY,
    guild_id INTEGER,
    added_by TEXT,
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Achievement unlocks are a LEDGER, not derived state: once granted, a badge
-- is never revoked by later data corrections or threshold tuning (services/
-- achievements.py derives candidate state; this records what was announced).
-- handle is the canonical post-merge handle at grant time; reads must span a
-- player's whole merge group. earned_at = played_at of the unlocking match.
CREATE TABLE IF NOT EXISTS achievement_unlocks (
    handle TEXT NOT NULL,
    key TEXT NOT NULL,
    earned_at TEXT NOT NULL,
    granted_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (handle, key)
);

-- Deployment facts that are data, not code. achievement_epoch is stamped the
-- first time this DB is opened by achievement-aware code: moment achievements
-- only count games played after it, so each deployment gets its own launch
-- date and imported history never mass-unlocks single-game feats.
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO meta (key, value) VALUES ('achievement_epoch', datetime('now'));
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
def _migration_3_repick_from(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(match_players)")}
    if "repick_from" not in cols:
        conn.execute("ALTER TABLE match_players ADD COLUMN repick_from TEXT")


def _migration_4_resources_killed(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(match_players)")}
    if "resources_killed" not in cols:
        conn.execute("ALTER TABLE match_players ADD COLUMN resources_killed INTEGER")


_AWARD_STAT_COLUMNS = ("econ_killed", "tech_killed", "resources_lost", "resources_floated")


def _migration_5_award_stats(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(match_players)")}
    for col in _AWARD_STAT_COLUMNS:
        if col not in cols:
            conn.execute(f"ALTER TABLE match_players ADD COLUMN {col} INTEGER")


def _migration_6_game_swings(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(matches)")}
    for col in ("comeback_deficit", "lead_changes"):
        if col not in cols:
            conn.execute(f"ALTER TABLE matches ADD COLUMN {col} INTEGER")


def _migration_7_drop_account_merges(conn: sqlite3.Connection) -> None:
    """Admin merges were replaced by Discord links (the account's member is
    the source of truth for identity); the legacy table goes away."""
    conn.execute("DROP TABLE IF EXISTS account_merges")


def _migration_8_achievements(conn: sqlite3.Connection) -> None:
    """Everything the achievements feature added, in one step: the unlock
    ledger, the meta table (see _SCHEMA — the epoch stamp itself is in
    _SCHEMA's INSERT OR IGNORE, which runs on every open, so a DB migrated
    here gets stamped the same way a fresh one does), and the two per-player
    replay stats that feed Special Delivery and Great Wall."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS achievement_unlocks (
               handle TEXT NOT NULL,
               key TEXT NOT NULL,
               earned_at TEXT NOT NULL,
               granted_at TEXT NOT NULL DEFAULT (datetime('now')),
               PRIMARY KEY (handle, key)
           )"""
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(match_players)")}
    for col in ("drop_commands", "static_defense", "bases_before_unit", "orbitals"):
        if col not in cols:
            conn.execute(f"ALTER TABLE match_players ADD COLUMN {col} INTEGER")


_MIGRATIONS = {
    1: _migration_1_content_key,
    2: _migration_2_replay_channels,
    3: _migration_3_repick_from,
    4: _migration_4_resources_killed,
    5: _migration_5_award_stats,
    6: _migration_6_game_swings,
    7: _migration_7_drop_account_merges,
    8: _migration_8_achievements,
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
                    winner_confidence, winner_method, comeback_deficit, lead_changes, uploaded_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            match.comeback_deficit,
            match.lead_changes,
        )

    def _insert_players(self, match_id: int, match: MonobattleMatch) -> None:
        self._conn.executemany(
            """INSERT INTO match_players
               (match_id, name, toon_handle, team, race, pick, repick_used, repick_from, resources_killed,
                econ_killed, tech_killed, resources_lost, resources_floated, drop_commands, static_defense,
                bases_before_unit, orbitals, unit_counts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    match_id,
                    p.name,
                    p.toon_handle,
                    p.team,
                    p.race,
                    p.pick,
                    None if p.repick_used is None else int(p.repick_used),
                    p.repick_from,
                    p.resources_killed,
                    p.econ_killed,
                    p.tech_killed,
                    p.resources_lost,
                    p.resources_floated,
                    p.drop_commands,
                    p.static_defense,
                    p.bases_before_unit,
                    p.orbitals,
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
                 winning_team = ?, winner_confidence = ?, winner_method = ?,
                 comeback_deficit = ?, lead_changes = ?, uploaded_by = ?
               WHERE id = ?""",
            (file_hash, key, *self._match_columns(match), uploaded_by, match_id),
        )
        self._conn.execute("DELETE FROM match_players WHERE match_id = ?", (match_id,))
        self._insert_players(match_id, match)
        self._conn.commit()

    def refresh_parse(self, match: MonobattleMatch, file_hash: str) -> bool:
        """Overwrite a stored game with a fresh parse of the SAME replay file
        (after parser improvements), keeping a manually confirmed winner and
        the original uploader. False if this exact file isn't stored."""
        row = self._conn.execute(
            "SELECT id, content_key, winning_team, winner_method, uploaded_by FROM matches WHERE file_hash = ?",
            (file_hash,),
        ).fetchone()
        if row is None:
            return False
        if row["winner_method"] == "confirmed":
            match = match.model_copy(
                update={"winning_team": row["winning_team"], "winner_confidence": 1.0, "winner_method": "confirmed"}
            )
        self._replace_match(row["id"], match, file_hash, row["content_key"], row["uploaded_by"])
        self.change_count += 1
        return True

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

    def release_name(self, sc2_name: str) -> str | None:
        """Delete a link by name regardless of owner (admin). Falls back to
        matching bound accounts by their in-game aliases, since second-account
        claim rows store the handle, not the display name. Returns the Discord
        id it was linked to, or None if nothing matched."""
        owner = self.discord_id_for(sc2_name)
        if owner is not None:
            self._conn.execute("DELETE FROM player_links WHERE sc2_name = ? COLLATE NOCASE", (sc2_name,))
            self._conn.commit()
            self.change_count += 1
            return owner
        for handle in self.handles_for_name(sc2_name):
            handle_owner = self.discord_id_for_handle(handle)
            if handle_owner is not None:
                self._conn.execute("DELETE FROM player_links WHERE toon_handle = ?", (handle,))
                self._conn.commit()
                self.change_count += 1
                return handle_owner
        return None

    def unlink_player(self, discord_id: str, sc2_name: str) -> bool:
        """Release one of the user's links by name; returns False if none
        matched. Falls back to matching their bound accounts by in-game alias
        (second-account claim rows store the handle, not the display name)."""
        cur = self._conn.execute(
            "DELETE FROM player_links WHERE sc2_name = ? COLLATE NOCASE AND discord_id = ?",
            (sc2_name, discord_id),
        )
        if cur.rowcount == 0:
            handles = self.handles_for_name(sc2_name)
            if handles:
                placeholders = ",".join("?" * len(handles))
                cur = self._conn.execute(
                    f"DELETE FROM player_links WHERE discord_id = ? AND toon_handle IN ({placeholders})",
                    (discord_id, *handles),
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

    def record_unlocks(self, rows: list[tuple[str, str, str]]) -> None:
        """Append (handle, key, earned_at_iso) rows to the unlock ledger.
        Append-only and idempotent: re-granting an already-held badge is a
        no-op, and nothing here ever deletes — unlocks are never revoked."""
        self._conn.executemany(
            "INSERT OR IGNORE INTO achievement_unlocks (handle, key, earned_at) VALUES (?, ?, ?)",
            rows,
        )
        self._conn.commit()

    def unlocks_for(self, handles: list[str]) -> list[tuple[str, str]]:
        """(key, earned_at) unlocked by any handle in a merge group, deduped
        keeping the earliest earn (accounts merged later may share badges)."""
        if not handles:
            return []
        placeholders = ",".join("?" * len(handles))
        rows = self._conn.execute(
            f"""SELECT key, MIN(earned_at) AS earned_at FROM achievement_unlocks
               WHERE handle IN ({placeholders}) GROUP BY key""",
            handles,
        ).fetchall()
        return [(r["key"], r["earned_at"]) for r in rows]

    def all_unlocks(self) -> list[tuple[str, str]]:
        """(handle, key) for every unlock — holder counts are computed from
        this, collapsing merge groups at read time."""
        rows = self._conn.execute("SELECT handle, key FROM achievement_unlocks").fetchall()
        return [(r["handle"], r["key"]) for r in rows]

    def unlock_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM achievement_unlocks").fetchone()[0]

    def upload_count(self, uploader: str) -> int:
        """Matches this uploader contributed. New ingests store the Discord
        user ID; historic rows hold display strings, which never match an id
        — so counts effectively start when achievements launched."""
        return self._conn.execute("SELECT COUNT(*) FROM matches WHERE uploaded_by = ?", (uploader,)).fetchone()[0]

    def achievement_epoch(self) -> datetime.datetime:
        """When achievements launched for THIS database (stamped on first
        open by achievement-aware code). Moment achievements only count games
        played after it."""
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'achievement_epoch'").fetchone()
        return datetime.datetime.fromisoformat(row["value"])

    def get_meta(self, key: str) -> str | None:
        """Read a deployment fact from the meta key/value table (None if
        unset). For small runtime state that outlives a restart but isn't
        match data — e.g. the live queue message pointer."""
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        """Upsert a meta key/value. Not a match write, so it does not bump
        change_count (derived caches don't depend on it)."""
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

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

    def merge_map(self) -> dict[str, str]:
        """A person's multiple accounts collapse to one canonical handle (the
        smallest in the group) so they share a single rating. The Discord
        account is the source of truth: groups are exactly the handles linked
        to the same Discord user. Solo accounts are omitted (they map to
        themselves)."""
        groups: dict[str, list[str]] = {}
        for r in self._conn.execute("SELECT discord_id, toon_handle FROM player_links WHERE toon_handle IS NOT NULL"):
            groups.setdefault(r["discord_id"], []).append(r["toon_handle"])
        mapping: dict[str, str] = {}
        for handles in groups.values():
            if len(handles) > 1:
                root = min(handles)
                for h in handles:
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

    def h2h_records(
        self, group1: list[str], group2: list[str], confidence_gate: float, min_duration: int
    ) -> tuple[list[int], list[int], list[tuple[int, MonobattleMatch]]]:
        """Two merged account groups' shared history over decided matches:
        (wins [g1, g2] when opposed, [wins, losses] when teamed, and the
        opposed matches oldest-first)."""
        g1, g2 = set(group1), set(group2)
        vs, together, opposed = [0, 0], [0, 0], []
        for match_id, match in self.all_matches():
            if (
                match.winning_team is None
                or match.winner_confidence < confidence_gate
                or match.duration_seconds < min_duration
            ):
                continue
            p1 = next((p for p in match.players if p.toon_handle in g1), None)
            p2 = next((p for p in match.players if p.toon_handle in g2), None)
            if p1 is None or p2 is None:
                continue
            if p1.team == p2.team:
                together[0 if p1.team == match.winning_team else 1] += 1
            else:
                vs[0 if p1.team == match.winning_team else 1] += 1
                opposed.append((match_id, match))
        return vs, together, opposed

    def mvp_count(self, handles: list[str], confidence_gate: float, min_duration: int) -> int:
        """How many decided games any of these accounts was the MVP of."""
        group = set(handles)
        count = 0
        for _match_id, match in self.all_matches():
            if match.winner_confidence < confidence_gate or match.duration_seconds < min_duration:
                continue
            mvp = match.mvp()
            if mvp is not None and mvp.toon_handle in group:
                count += 1
        return count

    def award_counts(self, handles: list[str], confidence_gate: float, min_duration: int) -> dict[str, int]:
        """Career tally of stat awards (award key -> times won) for a merged
        account group, over rating-eligible games."""
        from services.awards import match_awards  # local import: awards also imports models

        group = set(handles)
        counts: dict[str, int] = {}
        for _match_id, match in self.all_matches():
            if match.winner_confidence < confidence_gate or match.duration_seconds < min_duration:
                continue
            for award in match_awards(match):
                if award.player.toon_handle in group:
                    counts[award.key] = counts.get(award.key, 0) + 1
        return counts

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
                repick_from=p["repick_from"],
                resources_killed=p["resources_killed"],
                econ_killed=p["econ_killed"],
                tech_killed=p["tech_killed"],
                resources_lost=p["resources_lost"],
                resources_floated=p["resources_floated"],
                drop_commands=p["drop_commands"],
                static_defense=p["static_defense"],
                bases_before_unit=p["bases_before_unit"],
                orbitals=p["orbitals"],
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
            comeback_deficit=row["comeback_deficit"],
            lead_changes=row["lead_changes"],
        )
