"""SQLite persistence for parsed matches (the source of truth; everything
else is derived).

Schema changes: bump SCHEMA_VERSION, add to _MIGRATIONS and update _SCHEMA to
match. Never rebuild the DB — player_links rows are user-written.
"""

import datetime
import hashlib
import json
import logging
import os
import sqlite3
import sys
from collections import Counter
from typing import NamedTuple

from models.replay import MapVersion, MatchPlayer, MonobattleMatch

logger = logging.getLogger(__name__)


class IngestResult(NamedTuple):
    # inserted | updated (a better recording replaced it) | duplicate | rejected
    status: str
    match_id: int | None
    reason: str | None = None
    # The weaker recording this ingest left out (incoming or replaced), whose
    # archived file can go.
    dropped_hash: str | None = None


# A stored game is a 4v4 with 2+ minutes of battle. From 2:00 to
# REMAKE_CALL_SECONDS a remake call in chat rejects it (rushes end in "gg" from
# 2:05; past 3:00 an "rm?" is post-game chatter). Checked over 241 short games
# (Nov 2026).
TEAM_SIZE = 4
MIN_BATTLE_SECONDS = 120
REMAKE_CALL_SECONDS = 180


def rejection_reason(match: MonobattleMatch) -> str | None:
    """Why this game shouldn't be stored, or None when it should. A game
    someone approved after it was turned away (match.approved) is kept."""
    if match.approved:
        return None
    sizes = Counter(p.team for p in match.players)
    if len(sizes) != 2 or any(n != TEAM_SIZE for n in sizes.values()):
        return f"it's a {match.game_type}, not a 4v4"
    if match.battle_seconds < MIN_BATTLE_SECONDS:
        return "it's a remake (under 2 minutes of play)"
    if match.battle_seconds < REMAKE_CALL_SECONDS and match.remake_called:
        return "it's a remake (called in chat, under 3 minutes in)"
    return None


class LinkResult(NamedTuple):
    # linked (handle bound if unambiguous) | taken | ambiguous (several accounts)
    status: str
    owner: str | None = None
    handle: str | None = None
    candidates: int = 0


class Season(NamedTuple):
    id: int
    name: str
    started_at: str  # ISO, inclusive
    ended_at: str | None  # ISO, exclusive; None = still running
    # Soft reset: ratings start from career at started_at, pulled toward the
    # prior (services.rating.RatingBook.seed). Both None = a hard reset.
    carryover: float | None = None
    seed_sigma: float | None = None

    @classmethod
    def from_row(cls, row) -> "Season":
        return cls(row["id"], row["name"], row["started_at"], row["ended_at"], row["carryover"], row["seed_sigma"])

    def contains(self, played_at: str) -> bool:
        return self.started_at <= played_at and (self.ended_at is None or played_at < self.ended_at)


DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "resources", "monobot.db")

# Stored in each DB file via PRAGMA user_version. Version 1 = the 2026-07
# baseline schema below; pre-versioning DBs read as 0 and are migrated up.
SCHEMA_VERSION = 20

_SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY,
    file_hash TEXT NOT NULL UNIQUE,
    content_key TEXT NOT NULL UNIQUE,
    file_name TEXT NOT NULL,
    map_name TEXT NOT NULL,
    map_hash TEXT NOT NULL DEFAULT '',
    played_at TEXT NOT NULL,
    game_seed INTEGER,
    duration_seconds INTEGER NOT NULL,
    game_type TEXT NOT NULL,
    pick_mode TEXT NOT NULL,
    pick_phase_seconds INTEGER NOT NULL,
    winning_team INTEGER,
    winner_confidence REAL NOT NULL,
    winner_method TEXT NOT NULL,
    comeback_deficit INTEGER,
    lead_changes INTEGER,
    remake_called INTEGER,
    approved INTEGER NOT NULL DEFAULT 0,
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
    lost_all_bases INTEGER,
    cannon_rush INTEGER,
    spine_rush INTEGER,
    unit_counts TEXT NOT NULL,
    kills_by_unit TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_match_players_match ON match_players(match_id);
CREATE INDEX IF NOT EXISTS idx_match_players_name ON match_players(name);
CREATE INDEX IF NOT EXISTS idx_match_players_handle ON match_players(toon_handle);
CREATE INDEX IF NOT EXISTS idx_matches_played_at ON matches(played_at);

-- The SC2 level a player reports before their first game here (beginner /
-- mid / top, see rating.SKILL_LEVEL_MU). Written by users like player_links,
-- so it survives everything a reparse touches. Only the walk's starting point
-- reads it: the player's first rated game starts from it instead of the prior.
CREATE TABLE IF NOT EXISTS skill_levels (
    discord_id TEXT PRIMARY KEY,
    level TEXT NOT NULL,
    set_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Achievement unlocks are a LEDGER, not derived state: once granted, a badge
-- is revoked only by a deliberate reconcile, never as a side effect (services/
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

-- Ladder seasons. A season is a TIME WINDOW, not a tag on each match: a
-- match belongs to the season whose window contains its played_at. That way a
-- replay from last season uploaded today still scores against last season,
-- and no stored row has to be rewritten when a season turns over. The open
-- season (ended_at IS NULL) is the current one; there is always exactly one.
-- Only ratings are season-scoped (see services/rating.py) — match history,
-- profile stats and the achievement ledger stay career-wide.
CREATE TABLE IF NOT EXISTS seasons (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    -- Soft-reset parameters; NULL = hard reset (everyone starts at the prior).
    carryover REAL,
    seed_sigma REAL
);

-- Exactly one open season. Indexing the expression, not ended_at itself:
-- SQLite treats NULLs as distinct, so a unique index on ended_at would
-- happily allow two open seasons.
CREATE UNIQUE INDEX IF NOT EXISTS idx_seasons_open
    ON seasons((ended_at IS NULL)) WHERE ended_at IS NULL;
-- Season 1 opens at the dawn of time so all pre-seasons history belongs to it.
INSERT OR IGNORE INTO seasons (id, name, started_at) VALUES (1, 'Season 1', '0001-01-01T00:00:00');

-- The terrain behind each published version of the arcade map. Every game is
-- played on the same arcade map, so matches.map_name is always "Monobattle
-- LotV - Map Rotation"; the rotation happens when the author republishes with
-- new terrain, changing matches.map_hash. The terrain's real name lives only
-- in the published map file, so it is fetched once per hash over the network
-- and cached here (name NULL = fetched, but the header named no map — a
-- negative cache, so a mute version isn't re-fetched forever).
CREATE TABLE IF NOT EXISTS map_versions (
    map_hash TEXT PRIMARY KEY,
    name TEXT,
    author TEXT,
    checked_at TEXT NOT NULL DEFAULT (datetime('now'))
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
    """The game's identity across recordings: its seed, else roster + start
    minute (straddled minutes are caught by _same_game)."""
    names = [p.name for p in match.players]
    if match.game_seed is not None:
        return hashlib.sha256(f"seed:{match.game_seed}|{'|'.join(sorted(names))}".encode()).hexdigest()
    return content_key_for(match.played_at, names)


def content_key_for(played_at: datetime.datetime, names: list[str]) -> str:
    """The pre-seed content key: start minute + roster."""
    minute = played_at.replace(second=0, microsecond=0)
    return hashlib.sha256(f"{minute.isoformat()}|{'|'.join(sorted(names))}".encode()).hexdigest()


class _Recording(NamedTuple):
    """What _same_game compares: one stored or incoming recording."""

    seed: int | None
    lineup: frozenset[tuple[str, int, str | None]]  # (toon_handle, team, pick)
    played_at: datetime.datetime
    duration_seconds: int


def _same_game(a: _Recording, b: _Recording) -> bool:
    """Same seed when both are known; else the same accounts, teams and picks
    starting less than a game-length apart (recorders' clocks disagree)."""
    if {h for h, _, _ in a.lineup} != {h for h, _, _ in b.lineup}:
        return False
    if a.seed is not None and b.seed is not None:
        return a.seed == b.seed
    gap = abs((a.played_at - b.played_at).total_seconds())
    return a.lineup == b.lineup and gap < min(a.duration_seconds, b.duration_seconds)


def _migration_1_content_key(conn: sqlite3.Connection) -> None:
    """Backfill content_key on DBs created before it existed. No-op on DBs
    that already have the column (the pre-versioning baseline)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(matches)")}
    if "content_key" not in cols:
        conn.execute("ALTER TABLE matches ADD COLUMN content_key TEXT")
        for row in conn.execute("SELECT id, played_at FROM matches").fetchall():
            names = [r["name"] for r in conn.execute("SELECT name FROM match_players WHERE match_id = ?", (row["id"],))]
            key = content_key_for(datetime.datetime.fromisoformat(row["played_at"]), names)
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


# version -> upgrade from version-1. Each must leave the DB identical to a
# fresh _SCHEMA at that version.
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
    """Achievements: the unlock ledger, the meta table (the epoch is stamped
    by _SCHEMA on open) and two per-player stats."""
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


def _migration_9_lost_all_bases(conn: sqlite3.Connection) -> None:
    """lost_all_bases (Nomad)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(match_players)")}
    if "lost_all_bases" not in cols:
        conn.execute("ALTER TABLE match_players ADD COLUMN lost_all_bases INTEGER")


def _migration_10_seasons(conn: sqlite3.Connection) -> None:
    """Introduce ladder seasons. Existing history all belongs to Season 1,
    whose window starts at the dawn of time — no match rows are touched."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS seasons (
               id INTEGER PRIMARY KEY,
               name TEXT NOT NULL,
               started_at TEXT NOT NULL,
               ended_at TEXT
           )"""
    )
    # Indexes the expression, not ended_at: NULLs are distinct in SQLite, so
    # a unique index on the column itself would allow two open seasons.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_seasons_open ON seasons((ended_at IS NULL)) WHERE ended_at IS NULL"
    )
    conn.execute("INSERT OR IGNORE INTO seasons (id, name, started_at) VALUES (1, 'Season 1', '0001-01-01T00:00:00')")


def _migration_11_map_versions(conn: sqlite3.Connection) -> None:
    """map_hash per game, and the map_versions name cache."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(matches)")}
    if "map_hash" not in cols:
        conn.execute("ALTER TABLE matches ADD COLUMN map_hash TEXT NOT NULL DEFAULT ''")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS map_versions (
               map_hash TEXT PRIMARY KEY,
               name TEXT,
               author TEXT,
               checked_at TEXT NOT NULL DEFAULT (datetime('now'))
           )"""
    )


def _migration_12_kills_by_unit(conn: sqlite3.Connection) -> None:
    """kills_by_unit (JSON)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(match_players)")}
    if "kills_by_unit" not in cols:
        conn.execute("ALTER TABLE match_players ADD COLUMN kills_by_unit TEXT NOT NULL DEFAULT '{}'")


def _migration_13_soft_season_reset(conn: sqlite3.Connection) -> None:
    """Soft-reset parameters; existing seasons stay NULL (hard resets)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(seasons)")}
    for col in ("carryover", "seed_sigma"):
        if col not in cols:
            conn.execute(f"ALTER TABLE seasons ADD COLUMN {col} REAL")


def _migration_14_cannon_rush(conn: sqlite3.Connection) -> None:
    """cannon_rush; NULL means not recorded, never "didn't rush"."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(match_players)")}
    if "cannon_rush" not in cols:
        conn.execute("ALTER TABLE match_players ADD COLUMN cannon_rush INTEGER")


def _migration_15_spine_rush(conn: sqlite3.Connection) -> None:
    """The Zerg counterpart of cannon_rush, with the same NULL meaning."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(match_players)")}
    if "spine_rush" not in cols:
        conn.execute("ALTER TABLE match_players ADD COLUMN spine_rush INTEGER")


def _migration_16_skill_levels(conn: sqlite3.Connection) -> None:
    """Self-reported SC2 levels, seeding a new player's first game."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS skill_levels (
               discord_id TEXT PRIMARY KEY,
               level TEXT NOT NULL,
               set_at TEXT NOT NULL DEFAULT (datetime('now'))
           )"""
    )


def _migration_17_approved(conn: sqlite3.Connection) -> None:
    """Games kept although they break rejection_reason's rules, because a
    player or admin approved them on upload — so a prune leaves them be."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(matches)")}
    if "approved" not in cols:
        conn.execute("ALTER TABLE matches ADD COLUMN approved INTEGER NOT NULL DEFAULT 0")


def _migration_18_merge_duplicate_recordings(conn: sqlite3.Connection) -> None:
    """Add game_seed and merge games stored twice under minute-based keys,
    keeping the recording ingest would (approvals carry over)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(matches)")}
    if "game_seed" not in cols:
        conn.execute("ALTER TABLE matches ADD COLUMN game_seed INTEGER")
    lineups: dict[int, set] = {}
    for r in conn.execute("SELECT match_id, toon_handle, team, pick FROM match_players"):
        lineups.setdefault(r["match_id"], set()).add((r["toon_handle"], r["team"], r["pick"]))
    by_accounts: dict[frozenset, list] = {}
    for r in conn.execute("SELECT * FROM matches ORDER BY played_at, id"):
        lineup = frozenset(lineups.get(r["id"], ()))
        by_accounts.setdefault(frozenset(h for h, _, _ in lineup), []).append(
            (
                _Recording(
                    r["game_seed"], lineup, datetime.datetime.fromisoformat(r["played_at"]), r["duration_seconds"]
                ),
                r,
            )
        )
    for games in by_accounts.values():
        clusters: list[list] = []
        for game in games:
            if clusters and _same_game(clusters[-1][0][0], game[0]):
                clusters[-1].append(game)
            else:
                clusters.append([game])
        for cluster in clusters:
            if len(cluster) < 2:
                continue
            rows = [row for _, row in cluster]
            keep = max(
                rows,
                key=lambda r: (r["winner_method"] == "confirmed", r["winner_confidence"], r["duration_seconds"]),
            )
            drop = [r["id"] for r in rows if r["id"] != keep["id"]]
            if any(r["approved"] for r in rows):
                conn.execute("UPDATE matches SET approved = 1 WHERE id = ?", (keep["id"],))
            conn.executemany("DELETE FROM match_players WHERE match_id = ?", [(i,) for i in drop])
            conn.executemany("DELETE FROM matches WHERE id = ?", [(i,) for i in drop])
            logger.info("Merged duplicate recordings of match %d: dropped %s", keep["id"], drop)


def _migration_19_drop_replay_channels(conn: sqlite3.Connection) -> None:
    """Watched channels come from config.json alone now (!watchreplays is
    gone). Logs any channel this drops, to be added to the config."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'replay_channels'").fetchone():
        for row in conn.execute("SELECT channel_id, guild_id FROM replay_channels"):
            logger.warning(
                "No longer watching replay channel %s (server %s): add it to config.json's replays_channel_ids",
                row["channel_id"],
                row["guild_id"],
            )
        conn.execute("DROP TABLE replay_channels")


def _migration_20_remake_called(conn: sqlite3.Connection) -> None:
    """MonobattleMatch.remake_called; NULL on games parsed before it was read."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(matches)")}
    if "remake_called" not in cols:
        conn.execute("ALTER TABLE matches ADD COLUMN remake_called INTEGER")


# Global Discord links, in the home DB. Other stores ATTACH it as LINKS_SCHEMA
# and read it unqualified (their own DB has no such table). "{s}" is the
# schema prefix: "" in the home DB, "links." when attached.
LINKS_SCHEMA = "links"
_LINKS_SCHEMA = """
-- Maps a Discord user to the SC2 name(s) they claim. sc2_name is the display
-- name they entered; toon_handle is SC2's unique account id, bound the first
-- time that name is seen in an uploaded game (NULL until then). Rating and
-- participant checks key on the handle, so two players who share a display
-- name never merge.
CREATE TABLE IF NOT EXISTS {s}player_links (
    discord_id TEXT NOT NULL,
    sc2_name TEXT NOT NULL,
    toon_handle TEXT,
    linked_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (sc2_name)
);

CREATE INDEX IF NOT EXISTS {s}idx_player_links_discord ON player_links(discord_id);
CREATE UNIQUE INDEX IF NOT EXISTS {s}idx_player_links_handle
    ON player_links(toon_handle) WHERE toon_handle IS NOT NULL;
-- Name claims and lookups are case-insensitive: SC2 names have fixed casing
-- but users won't reproduce it, and "Rain"/"rain" must not be two claims.
CREATE UNIQUE INDEX IF NOT EXISTS {s}idx_player_links_name_nocase
    ON player_links(sc2_name COLLATE NOCASE);
"""


def _interned_keys(d: dict) -> dict:
    return {sys.intern(k): v for k, v in d.items()}


class SharedLinks:
    """The links DB plus a write counter every sharing store's change_count
    includes, so a link invalidates every community's caches."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self.version = 0

    def held_by(self, db_path: str) -> bool:
        return _same_file(self.db_path, db_path)


def _same_file(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


_MIGRATIONS = {
    1: _migration_1_content_key,
    2: _migration_2_replay_channels,
    3: _migration_3_repick_from,
    4: _migration_4_resources_killed,
    5: _migration_5_award_stats,
    6: _migration_6_game_swings,
    7: _migration_7_drop_account_merges,
    8: _migration_8_achievements,
    9: _migration_9_lost_all_bases,
    10: _migration_10_seasons,
    11: _migration_11_map_versions,
    12: _migration_12_kills_by_unit,
    13: _migration_13_soft_season_reset,
    14: _migration_14_cannon_rush,
    15: _migration_15_spine_rush,
    16: _migration_16_skill_levels,
    17: _migration_17_approved,
    18: _migration_18_merge_duplicate_recordings,
    19: _migration_19_drop_replay_channels,
    20: _migration_20_remake_called,
}


class MatchStore:
    def __init__(self, db_path: str = DEFAULT_DB_PATH, links: SharedLinks | None = None):
        """Without `links` (scripts, tests) the store keeps its own links."""
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._db_path = db_path
        self._links = links or SharedLinks(db_path)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        # Several connections share the links DB, so a write can meet a lock.
        self._conn.execute("PRAGMA busy_timeout = 10000")
        fresh = self._conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'matches'").fetchone()
        self._conn.executescript(_SCHEMA)
        if self._links.held_by(db_path):
            self._conn.executescript(_LINKS_SCHEMA.format(s=""))
        else:
            self._conn.execute(f"ATTACH DATABASE ? AS {LINKS_SCHEMA}", (self._links.db_path,))
            self._conn.executescript(_LINKS_SCHEMA.format(s=f"{LINKS_SCHEMA}."))
        if fresh is None:
            # Brand-new DB: _SCHEMA created it at the latest version already.
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.commit()
        else:
            self._migrate()
        if not self._links.held_by(db_path):
            self._adopt_own_links()
        self._changes = 0
        self._outside_version = self._data_version()
        # (change number, games in play order, id -> game); see all_matches.
        self._games: tuple[int, list[tuple[int, MonobattleMatch]], dict[int, MonobattleMatch]] | None = None

    def _data_version(self) -> int:
        return self._conn.execute("PRAGMA data_version").fetchone()[0]

    def _note_outside_writes(self) -> None:
        """Count other connections' commits (e.g. reparse_stored.py) as changes.
        data_version ignores this connection's own."""
        version = self._data_version()
        if version != self._outside_version:
            self._outside_version = version
            self._changes += 1

    @property
    def change_count(self) -> int:
        """Moves on any write — ours, a shared link, or another process's — so
        caches know they're stale."""
        self._note_outside_writes()
        return self._changes + self._links.version

    def _adopt_own_links(self) -> None:
        """Move this DB's own links into the shared table and rename its table
        aside (it would shadow the attached one). Clashing rows stay in
        player_links_premerge and are logged."""
        own = self._conn.execute("SELECT 1 FROM main.sqlite_master WHERE type = 'table' AND name = 'player_links'")
        if own.fetchone() is None:
            return
        rows = self._conn.execute(
            "SELECT discord_id, sc2_name, toon_handle, linked_at FROM main.player_links"
        ).fetchall()
        moved = 0
        for r in rows:
            cur = self._conn.execute(
                f"INSERT OR IGNORE INTO {LINKS_SCHEMA}.player_links (discord_id, sc2_name, toon_handle, linked_at) "
                "VALUES (?, ?, ?, ?)",
                tuple(r),
            )
            if cur.rowcount:
                moved += 1
                continue
            same = self._conn.execute(
                f"SELECT 1 FROM {LINKS_SCHEMA}.player_links WHERE discord_id = ? AND sc2_name = ? COLLATE NOCASE",
                (r["discord_id"], r["sc2_name"]),
            ).fetchone()
            if same is None:
                logger.warning(
                    "%s: link %s -> %s clashes with a shared link; kept only in player_links_premerge",
                    self._db_path,
                    r["discord_id"],
                    r["sc2_name"],
                )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS main.player_links_premerge AS SELECT * FROM main.player_links WHERE 0"
        )
        self._conn.execute("INSERT INTO main.player_links_premerge SELECT * FROM main.player_links")
        self._conn.execute("DROP TABLE main.player_links")
        self._conn.commit()
        self._links.version += 1
        logger.info("%s: moved %d of %d links into the shared links table", self._db_path, moved, len(rows))

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
        """Store a match; for a game already stored keep the better recording
        (higher confidence, then longer). Confirmed winners are never
        overwritten."""
        reason = rejection_reason(match)
        if reason is not None:
            return IngestResult("rejected", None, reason)
        key = content_key(match)
        existing = self.find_game(match)

        if existing is not None:
            if existing["file_hash"] == file_hash:
                return IngestResult("duplicate", existing["id"])
            if not self._is_more_complete(match, existing):
                logger.info("Same game already stored, keeping better recording: %s", match.file_name)
                return IngestResult("duplicate", existing["id"], dropped_hash=file_hash)
            self._replace_match(existing["id"], match, file_hash, existing["content_key"], uploaded_by)
            self.bind_handles_from_match(match)
            self._changes += 1
            return IngestResult("updated", existing["id"], dropped_hash=existing["file_hash"])

        try:
            cur = self._conn.execute(
                """INSERT INTO matches
                   (file_hash, content_key, file_name, map_name, map_hash, played_at, game_seed, duration_seconds,
                    game_type, pick_mode, pick_phase_seconds, winning_team,
                    winner_confidence, winner_method, comeback_deficit, lead_changes, remake_called, approved,
                    uploaded_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
        self._changes += 1
        return IngestResult("inserted", match_id)

    def find_game(self, match: MonobattleMatch) -> sqlite3.Row | None:
        """The stored recording of this game, if any: by content_key, else
        _same_game. Read-only, so a preview can say what ingest would do."""
        existing = self._conn.execute(
            """SELECT id, file_hash, content_key, duration_seconds, winner_confidence, winner_method
               FROM matches WHERE content_key = ?""",
            (content_key(match),),
        ).fetchone()
        return existing if existing is not None else self._find_same_game(match)

    def ingest_preview(self, match: MonobattleMatch) -> str:
        """Dry-run ingest: "new", "better" or "stored"."""
        existing = self.find_game(match)
        if existing is None:
            return "new"
        return "better" if self._is_more_complete(match, existing) else "stored"

    def _find_same_game(self, match: MonobattleMatch) -> sqlite3.Row | None:
        """A stored recording of this game that content_key didn't find: one
        stored before seeds were read, or one whose start minute differs."""
        mine = _Recording(
            match.game_seed,
            frozenset((p.toon_handle, p.team, p.pick) for p in match.players),
            match.played_at,
            match.duration_seconds,
        )
        # Only a game starting within this one's length can qualify.
        window = datetime.timedelta(seconds=match.duration_seconds + 1)
        candidates = self._conn.execute(
            """SELECT id, file_hash, content_key, played_at, game_seed, duration_seconds, winner_confidence,
                      winner_method
               FROM matches WHERE played_at BETWEEN ? AND ?""",
            ((match.played_at - window).isoformat(), (match.played_at + window).isoformat()),
        ).fetchall()
        for row in candidates:
            lineup = frozenset(
                (r["toon_handle"], r["team"], r["pick"])
                for r in self._conn.execute(
                    "SELECT toon_handle, team, pick FROM match_players WHERE match_id = ?", (row["id"],)
                )
            )
            theirs = _Recording(
                row["game_seed"], lineup, datetime.datetime.fromisoformat(row["played_at"]), row["duration_seconds"]
            )
            if _same_game(mine, theirs):
                return row
        return None

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
            match.map_hash,
            match.played_at.isoformat(),
            match.game_seed,
            match.duration_seconds,
            match.game_type,
            match.pick_mode,
            match.pick_phase_seconds,
            match.winning_team,
            match.winner_confidence,
            match.winner_method,
            match.comeback_deficit,
            match.lead_changes,
            None if match.remake_called is None else int(match.remake_called),
            int(match.approved),
        )

    def _insert_players(self, match_id: int, match: MonobattleMatch) -> None:
        self._conn.executemany(
            """INSERT INTO match_players
               (match_id, name, toon_handle, team, race, pick, repick_used, repick_from, resources_killed,
                econ_killed, tech_killed, resources_lost, resources_floated, drop_commands, static_defense,
                bases_before_unit, orbitals, lost_all_bases, cannon_rush, spine_rush, unit_counts,
                kills_by_unit)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    None if p.lost_all_bases is None else int(p.lost_all_bases),
                    None if p.cannon_rush is None else int(p.cannon_rush),
                    None if p.spine_rush is None else int(p.spine_rush),
                    json.dumps(p.unit_counts),
                    json.dumps(p.kills_by_unit),
                )
                for p in match.players
            ],
        )

    def _replace_match(
        self, match_id: int, match: MonobattleMatch, file_hash: str, key: str, uploaded_by: str | None
    ) -> None:
        self._conn.execute(
            """UPDATE matches SET
                 file_hash = ?, content_key = ?, file_name = ?, map_name = ?, map_hash = ?, played_at = ?,
                 game_seed = ?, duration_seconds = ?, game_type = ?, pick_mode = ?, pick_phase_seconds = ?,
                 winning_team = ?, winner_confidence = ?, winner_method = ?,
                 comeback_deficit = ?, lead_changes = ?, remake_called = ?, approved = ?, uploaded_by = ?
               WHERE id = ?""",
            (file_hash, key, *self._match_columns(match), uploaded_by, match_id),
        )
        self._conn.execute("DELETE FROM match_players WHERE match_id = ?", (match_id,))
        self._insert_players(match_id, match)
        self._conn.commit()

    def delete_matches(self, match_ids: list[int]) -> int:
        """Delete matches (players cascade); the next reconcile revokes badges."""
        cur = self._conn.executemany("DELETE FROM matches WHERE id = ?", [(i,) for i in match_ids])
        self._conn.commit()
        self._changes += 1
        return cur.rowcount

    def file_hashes(self) -> set[str]:
        """Every stored replay's file hash, so a backfill can skip parsing
        files this database doesn't hold."""
        return {row[0] for row in self._conn.execute("SELECT file_hash FROM matches")}

    def refresh_parse(self, match: MonobattleMatch, file_hash: str) -> bool:
        """Replace a stored game with a reparse of the same file, keeping a
        confirmed winner, uploader and approval. False if not stored."""
        row = self._conn.execute(
            "SELECT id, content_key, winning_team, winner_method, approved, uploaded_by FROM matches WHERE file_hash = ?",
            (file_hash,),
        ).fetchone()
        if row is None:
            return False
        if row["winner_method"] == "confirmed":
            match = match.model_copy(
                update={"winning_team": row["winning_team"], "winner_confidence": 1.0, "winner_method": "confirmed"}
            )
        if row["approved"]:
            match = match.model_copy(update={"approved": True})
        self._replace_match(row["id"], match, file_hash, row["content_key"], row["uploaded_by"])
        self._changes += 1
        return True

    # -- map versions ----------------------------------------------------

    def record_map_version(self, map_hash: str, version: MapVersion | None) -> None:
        """None caches a failed lookup so it isn't retried."""
        self._conn.execute(
            """INSERT INTO map_versions (map_hash, name, author, checked_at)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(map_hash) DO UPDATE SET
                 name = excluded.name, author = excluded.author, checked_at = excluded.checked_at""",
            (map_hash, version.name if version else None, version.author if version else None),
        )
        self._conn.commit()
        self._changes += 1

    def unresolved_map_hashes(self) -> list[str]:
        """Map versions seen in history that have never been looked up."""
        rows = self._conn.execute(
            """SELECT DISTINCT m.map_hash FROM matches m
               LEFT JOIN map_versions v ON v.map_hash = m.map_hash
               WHERE m.map_hash != '' AND v.map_hash IS NULL"""
        ).fetchall()
        return [r["map_hash"] for r in rows]

    def map_version_names(self) -> dict[str, str]:
        """hash -> terrain name, for every version resolved so far. Callers
        fall back to the match's own map_name for anything missing."""
        rows = self._conn.execute("SELECT map_hash, name FROM map_versions WHERE name IS NOT NULL").fetchall()
        return {r["map_hash"]: r["name"] for r in rows}

    def set_game_seeds(self, seeds: dict[str, int]) -> int:
        """Set game_seed by file hash, touching nothing else; returns how many."""
        cur = self._conn.executemany(
            "UPDATE matches SET game_seed = ? WHERE file_hash = ? AND game_seed IS NULL",
            [(seed, file_hash) for file_hash, seed in seeds.items()],
        )
        self._conn.commit()
        self._changes += 1
        return cur.rowcount

    def map_hash_samples(self) -> list[tuple[str, str]]:
        """(played_at, map_hash) for every game whose version is known,
        oldest first — the timeline of which map was live when."""
        rows = self._conn.execute(
            "SELECT played_at, map_hash FROM matches WHERE map_hash != '' ORDER BY played_at"
        ).fetchall()
        return [(r["played_at"], r["map_hash"]) for r in rows]

    def link_player(self, discord_id: str, sc2_name: str) -> LinkResult:
        """Claim a name, binding its account if unambiguous (or on first game)."""
        owner = self.discord_id_for(sc2_name)
        if owner is not None and owner != discord_id:
            return LinkResult("taken", owner=owner)
        if owner is None:
            self._conn.execute(
                "INSERT INTO player_links (discord_id, sc2_name) VALUES (?, ?)",
                (discord_id, sc2_name),
            )
            self._links.version += 1
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
        self._links.version += 1
        return True

    def add_account(self, discord_id: str, handle: str) -> bool:
        """Bind another account by handle (stored as its own claim name). False
        if someone else holds it."""
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
        self._links.version += 1
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

    def handles_by_current_name(self, sc2_name: str) -> list[str]:
        """Accounts called this in their latest game, most-played first."""
        rows = self._conn.execute(
            """SELECT mp.toon_handle AS h, COUNT(*) AS games,
                      (SELECT mp2.name FROM match_players mp2 JOIN matches m2 ON m2.id = mp2.match_id
                        WHERE mp2.toon_handle = mp.toon_handle
                        ORDER BY m2.played_at DESC LIMIT 1) AS current_name
               FROM match_players mp
               WHERE mp.toon_handle != ''
               GROUP BY mp.toon_handle
               HAVING current_name = ? COLLATE NOCASE
               ORDER BY games DESC""",
            (sc2_name,),
        ).fetchall()
        return [r["h"] for r in rows]

    def game_count_for_handles(self, handles: list[str]) -> int:
        """Games played by a whole merge group."""
        if not handles:
            return 0
        placeholders = ",".join("?" * len(handles))
        row = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM match_players WHERE toon_handle IN ({placeholders})", handles
        ).fetchone()
        return row["n"]

    def _try_bind(self, sc2_name: str) -> None:
        """Bind an unbound link for sc2_name iff the name maps to exactly one
        account across all stored matches (else it's ambiguous — leave it)."""
        handles = self.handles_for_name(sc2_name)
        if len(handles) != 1:
            return
        try:
            cur = self._conn.execute(
                "UPDATE player_links SET toon_handle = ? WHERE sc2_name = ? COLLATE NOCASE AND toon_handle IS NULL",
                (handles[0], sc2_name),
            )
            if cur.rowcount:
                self._links.version += 1  # a new binding can change merge_map
        except sqlite3.IntegrityError:
            pass  # handle already bound to another claimed name; leave as-is

    def unlink_player(self, discord_id: str, sc2_name: str) -> bool:
        """Release a link by claim name or a bound account's alias."""
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
            self._links.version += 1
            return True
        return False

    def record_unlocks(self, rows: list[tuple[str, str, str]]) -> None:
        """Idempotent insert of (handle, key, earned_at_iso)."""
        self._conn.executemany(
            "INSERT OR IGNORE INTO achievement_unlocks (handle, key, earned_at) VALUES (?, ?, ?)",
            rows,
        )
        self._conn.commit()

    def revoke_unlocks(self, rows: list[tuple[str, str]]) -> int:
        """Delete exactly these (handle, key) unlocks; returns rows removed. An
        explicit list, never a rule — see engine.reconcile."""
        if not rows:
            return 0
        cur = self._conn.executemany("DELETE FROM achievement_unlocks WHERE handle = ? AND key = ?", rows)
        self._conn.commit()
        self._changes += 1
        return cur.rowcount

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

    def unlocks_between(self, since: datetime.datetime, until: datetime.datetime) -> list[tuple[str, str]]:
        """Unlocks whose game was played in [since, until] (UTC; earned_at is
        stored naive)."""
        bounds = [b.astimezone(datetime.UTC).replace(tzinfo=None).isoformat() for b in (since, until)]
        rows = self._conn.execute(
            "SELECT handle, key FROM achievement_unlocks WHERE earned_at >= ? AND earned_at <= ?", bounds
        ).fetchall()
        return [(r["handle"], r["key"]) for r in rows]

    def all_unlocks(self) -> list[tuple[str, str]]:
        """(handle, key) for every unlock — holder counts are computed from
        this, collapsing merge groups at read time."""
        rows = self._conn.execute("SELECT handle, key FROM achievement_unlocks").fetchall()
        return [(r["handle"], r["key"]) for r in rows]

    def unlock_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM achievement_unlocks").fetchone()[0]

    def discovered_keys(self) -> set[str]:
        """Keys anyone holds (a secret's name shows once discovered)."""
        return {r["key"] for r in self._conn.execute("SELECT DISTINCT key FROM achievement_unlocks")}

    def upload_count(self, uploader: str) -> int:
        """By Discord id; old rows hold display names, so counts start at launch."""
        return self._conn.execute("SELECT COUNT(*) FROM matches WHERE uploaded_by = ?", (uploader,)).fetchone()[0]

    def achievement_epoch(self) -> datetime.datetime:
        """When achievements launched here; moment specs count only later games."""
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'achievement_epoch'").fetchone()
        return datetime.datetime.fromisoformat(row["value"])

    def get_meta(self, key: str) -> str | None:
        """Small runtime state that outlives a restart; None if unset."""
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
        self._changes += 1

    # -- reads -----------------------------------------------------------

    def sc2_names_for(self, discord_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT sc2_name FROM player_links WHERE discord_id = ? ORDER BY sc2_name", (discord_id,)
        ).fetchall()
        return [r["sc2_name"] for r in rows]

    def skill_level(self, discord_id: str) -> str | None:
        row = self._conn.execute("SELECT level FROM skill_levels WHERE discord_id = ?", (discord_id,)).fetchone()
        return row["level"] if row else None

    def set_skill_level(self, discord_id: str, level: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO skill_levels (discord_id, level) VALUES (?, ?) "
                "ON CONFLICT(discord_id) DO UPDATE SET level = excluded.level, set_at = datetime('now')",
                (discord_id, level),
            )
        self._changes += 1

    def skill_levels_by_handle(self) -> dict[str, str]:
        """Each linked account's owner's reported level, by toon_handle."""
        rows = self._conn.execute(
            "SELECT l.toon_handle, s.level FROM player_links l JOIN skill_levels s ON s.discord_id = l.discord_id "
            "WHERE l.toon_handle IS NOT NULL"
        )
        return {r["toon_handle"]: r["level"] for r in rows}

    def is_approved(self, file_hash: str) -> bool:
        """Whether this replay file is stored as an approved rule-breaker."""
        row = self._conn.execute("SELECT approved FROM matches WHERE file_hash = ?", (file_hash,)).fetchone()
        return bool(row and row["approved"])

    def handles_for(self, discord_id: str) -> list[str]:
        """The SC2 account handles bound to this user (names they've played
        under at least once). Empty if linked-but-never-played."""
        rows = self._conn.execute(
            "SELECT toon_handle FROM player_links WHERE discord_id = ? AND toon_handle IS NOT NULL ORDER BY toon_handle",
            (discord_id,),
        ).fetchall()
        return [r["toon_handle"] for r in rows]

    def merge_map(self) -> dict[str, str]:
        """handle -> canonical handle (smallest of the Discord user's handles).
        Solo accounts are omitted."""
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
        """The whole merge group, itself included."""
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

    def all_matches(self, since: str | None = None, until: str | None = None) -> list[tuple[int, MonobattleMatch]]:
        """(id, match) oldest first; `since`/`until` bound played_at [inclusive,
        exclusive). The games are built once per change and shared between
        callers (~200 MB at thousands): never modify one."""
        _, games, by_id = self._all_games()
        if since is None and until is None:
            return list(games)
        clauses, params = [], []
        if since is not None:
            clauses.append("played_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("played_at < ?")
            params.append(until)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        ids = [
            row["id"] for row in self._conn.execute(f"SELECT id FROM matches {where} ORDER BY played_at, id", params)
        ]
        if any(i not in by_id for i in ids):
            # Another process (a script run beside the bot) wrote games this
            # store never counted as a change: rebuild rather than miss them.
            _, _, by_id = self._all_games(force=True)
        return [(i, by_id[i]) for i in ids]

    def _all_games(
        self, force: bool = False
    ) -> tuple[int, list[tuple[int, MonobattleMatch]], dict[int, MonobattleMatch]]:
        self._note_outside_writes()
        if force or self._games is None or self._games[0] != self._changes:
            players: dict[int, list[sqlite3.Row]] = {}
            for p in self._conn.execute("SELECT * FROM match_players ORDER BY match_id, team, name"):
                players.setdefault(p["match_id"], []).append(p)
            rows = self._conn.execute("SELECT * FROM matches ORDER BY played_at, id").fetchall()
            games = [(row["id"], self._build_match(row, players.get(row["id"], []))) for row in rows]
            self._games = (self._changes, games, dict(games))
        return self._games

    def match_numbers(self) -> dict[int, int]:
        """id -> game number (place in play order, from #1). A late old upload
        shifts later numbers."""
        rows = self._conn.execute("SELECT id FROM matches ORDER BY played_at, id")
        return {row["id"]: n for n, row in enumerate(rows, 1)}

    def season_matches(self, season: "Season") -> list[tuple[int, MonobattleMatch]]:
        return self.all_matches(since=season.started_at, until=season.ended_at)

    def find_season(self, query: str) -> Season | None:
        """ "Season 1", "1", "s1" or "#1", any case."""
        q = query.strip().lower()
        if not q:
            return None
        seasons = self.seasons()
        for s in seasons:
            if s.name.lower() == q:
                return s
        digits = q.lstrip("#s ").strip()
        if digits.isdigit():
            n = int(digits)
            # Number means the Nth season chronologically, which is what a
            # reader means by "season 2" even if the row was renamed.
            if 1 <= n <= len(seasons):
                return seasons[n - 1]
        return None

    def current_season(self) -> Season:
        """The open season. Guaranteed to exist — the schema seeds Season 1."""
        row = self._conn.execute(
            "SELECT * FROM seasons WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return Season.from_row(row)

    def season_containing(self, played_at: str) -> Season | None:
        """The season whose window holds this played_at."""
        return next((s for s in self.seasons() if s.contains(played_at)), None)

    def seasons(self) -> list[Season]:
        """All seasons, oldest first."""
        rows = self._conn.execute("SELECT * FROM seasons ORDER BY started_at").fetchall()
        return [Season.from_row(r) for r in rows]

    def start_season(self, name: str, carryover: float | None = None, seed_sigma: float | None = None) -> Season:
        """Close the open season and start one at the same instant. `carryover`
        and `seed_sigma` (both or neither) make it a soft reset."""
        if (carryover is None) != (seed_sigma is None):
            raise ValueError("a soft reset needs both carryover and seed_sigma")
        # UTC like played_at: windows are compared as strings in SQL.
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
        with self._conn:
            self._conn.execute("UPDATE seasons SET ended_at = ? WHERE ended_at IS NULL", (now,))
            cur = self._conn.execute(
                "INSERT INTO seasons (name, started_at, carryover, seed_sigma) VALUES (?, ?, ?, ?)",
                (name, now, carryover, seed_sigma),
            )
        self._changes += 1
        return Season(cur.lastrowid, name, now, None, carryover, seed_sigma)

    def pending_confirmations(self, confidence_gate: float, min_duration: int) -> list[tuple[int, MonobattleMatch]]:
        """Real matches whose winner is unknown or below the rating gate."""
        rows = self._conn.execute(
            """SELECT * FROM matches
               WHERE (winning_team IS NULL OR winner_confidence < ?)
                 AND (duration_seconds >= ? OR approved = 1)
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
                or (match.duration_seconds < min_duration and not match.approved)
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
            if match.winner_confidence < confidence_gate or (
                match.duration_seconds < min_duration and not match.approved
            ):
                continue
            mvp = match.mvp()
            if mvp is not None and mvp.toon_handle in group:
                count += 1
        return count

    def mvp_counts(
        self, confidence_gate: float, min_duration: int, merge_map: dict[str, str] | None = None
    ) -> dict[str, int]:
        """MVPs over decided games by canonical handle (computed in Python)."""
        merge = merge_map or {}
        counts: dict[str, int] = {}
        for _match_id, match in self.all_matches():
            if match.winner_confidence < confidence_gate or (
                match.duration_seconds < min_duration and not match.approved
            ):
                continue
            mvp = match.mvp()
            if mvp is None:  # no kill stats (a handful of pre-archive parses)
                continue
            handle = merge.get(mvp.toon_handle, mvp.toon_handle)
            counts[handle] = counts.get(handle, 0) + 1
        return counts

    def award_counts(self, handles: list[str], confidence_gate: float, min_duration: int) -> dict[str, int]:
        """Career tally of stat awards (award key -> times won) for a merged
        account group, over rating-eligible games."""
        from services.awards import match_awards  # local import: awards also imports models

        group = set(handles)
        counts: dict[str, int] = {}
        for _match_id, match in self.all_matches():
            if match.winner_confidence < confidence_gate or (
                match.duration_seconds < min_duration and not match.approved
            ):
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
                 AND (m.duration_seconds >= ? OR m.approved = 1)
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

    def picks(self) -> set[str]:
        """Every unit anyone has picked, in its stored spelling ("WidowMine")."""
        rows = self._conn.execute("SELECT DISTINCT pick FROM match_players WHERE pick IS NOT NULL AND pick != ''")
        return {row["pick"] for row in rows}

    # -- internals -------------------------------------------------------

    def _build_match(self, row: sqlite3.Row, player_rows: list[sqlite3.Row] | None = None) -> MonobattleMatch:
        if player_rows is None:
            player_rows = self._conn.execute(
                "SELECT * FROM match_players WHERE match_id = ? ORDER BY team, name", (row["id"],)
            ).fetchall()
        # model_construct: validated when stored, and revalidating dominated load
        # time and memory. Interning shares the heavily repeated strings.
        players = [
            MatchPlayer.model_construct(
                set(),
                name=sys.intern(p["name"]),
                toon_handle=sys.intern(p["toon_handle"]),
                team=p["team"],
                race=sys.intern(p["race"]) if p["race"] else p["race"],
                pick=sys.intern(p["pick"]) if p["pick"] else p["pick"],
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
                lost_all_bases=None if p["lost_all_bases"] is None else bool(p["lost_all_bases"]),
                cannon_rush=None if p["cannon_rush"] is None else bool(p["cannon_rush"]),
                spine_rush=None if p["spine_rush"] is None else bool(p["spine_rush"]),
                unit_counts=_interned_keys(json.loads(p["unit_counts"])),
                kills_by_unit=_interned_keys(json.loads(p["kills_by_unit"])),
            )
            for p in player_rows
        ]
        return MonobattleMatch.model_construct(
            set(),
            file_name=row["file_name"],
            map_name=row["map_name"],
            map_hash=row["map_hash"],
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
            remake_called=None if row["remake_called"] is None else bool(row["remake_called"]),
            approved=bool(row["approved"]),
        )
