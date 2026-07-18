"""SQLite persistence for parsed matches.

The `matches` + `match_players` tables are the source of truth; ratings are
derived by replaying stored matches through RatingBook (see services.rating),
so parser/rating improvements can always be re-applied to history without
re-parsing replay files.

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
"""


def hash_replay(data: bytes) -> str:
    """Content hash used to deduplicate uploads of the same replay file."""
    return hashlib.sha256(data).hexdigest()


def content_key(match: MonobattleMatch) -> str:
    """Identity of the actual game, invariant across who recorded it. Two
    players' recordings of the same game differ in bytes and length (they may
    leave at different times) but share the start time and full roster.
    Minute precision absorbs sub-second clock differences between clients."""
    minute = match.played_at.replace(second=0, microsecond=0)
    names = "|".join(sorted(p.name for p in match.players))
    return hashlib.sha256(f"{minute.isoformat()}|{names}".encode()).hexdigest()


class MatchStore:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._migrate_content_key()
        # Bumped on every write so callers (e.g. the leaderboard cog) can
        # cache derived data like rating books and know when it's stale.
        self.change_count = 0

    def _migrate_content_key(self) -> None:
        """Backfill content_key on DBs created before it existed."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(matches)")}
        if "content_key" in cols:
            return
        logger.info("Migrating matches table: adding content_key")
        self._conn.execute("ALTER TABLE matches ADD COLUMN content_key TEXT")
        for match_id, match in self.all_matches():
            self._conn.execute("UPDATE matches SET content_key = ? WHERE id = ?", (content_key(match), match_id))
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_matches_content_key ON matches(content_key)")
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
        """Handles linked to the same Discord user collapse to one canonical
        handle, so a person's multiple accounts (cross-region, alt) share a
        single rating. Solo accounts are omitted (they map to themselves)."""
        groups: dict[str, list[str]] = {}
        for r in self._conn.execute("SELECT discord_id, toon_handle FROM player_links WHERE toon_handle IS NOT NULL"):
            groups.setdefault(r["discord_id"], []).append(r["toon_handle"])
        mapping: dict[str, str] = {}
        for handles in groups.values():
            if len(handles) > 1:
                canon = min(handles)
                for h in handles:
                    mapping[h] = canon
        return mapping

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
        self, handle: str, column: str, confidence_gate: float, min_duration: int
    ) -> dict[str, list[int]]:
        """For one account, wins/losses grouped by 'race' or 'pick', over
        decided matches. Returns {value: [wins, losses]}."""
        if column not in ("race", "pick"):
            raise ValueError(f"unsupported column: {column}")
        rows = self._conn.execute(
            f"""SELECT mp.{column} AS val, (mp.team = m.winning_team) AS won, COUNT(*) AS n
               FROM match_players mp JOIN matches m ON m.id = mp.match_id
               WHERE mp.toon_handle = ?
                 AND m.winning_team IS NOT NULL
                 AND m.winner_confidence >= ?
                 AND m.duration_seconds >= ?
                 AND mp.{column} IS NOT NULL
               GROUP BY mp.{column}, won""",
            (handle, confidence_gate, min_duration),
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
