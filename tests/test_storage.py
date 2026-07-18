import datetime

import pytest
from models.replay import MatchPlayer, MonobattleMatch
from services.rating import MIN_DURATION_SECONDS, MIN_WINNER_CONFIDENCE, RatingBook
from services.storage import MatchStore, content_key, hash_replay

BASE = datetime.datetime(2026, 7, 17, 12, 0, tzinfo=datetime.timezone.utc)


def _match(
    winning_team=1,
    confidence=1.0,
    method="recorded",
    duration=900,
    played_at=None,
    file_name="test.SC2Replay",
    roster=None,
):
    names = roster or ["A0", "A1", "A2", "A3", "B0", "B1", "B2", "B3"]
    players = [
        MatchPlayer(
            name=n,
            toon_handle=f"h-{n}",
            team=(1 if i < 4 else 2),
            race="Zerg",
            pick="Zergling" if i < 4 else "Marine",
            repick_used=(i % 4 == 0),
            unit_counts={"Zergling": 100} if i < 4 else {"Marine": 80},
        )
        for i, n in enumerate(names)
    ]
    return MonobattleMatch(
        file_name=file_name,
        map_name="Monobattle LotV - Map Rotation",
        played_at=played_at or BASE,
        duration_seconds=duration,
        game_type="4v4",
        pick_mode="blind_random",
        pick_phase_seconds=63,
        players=players,
        winning_team=winning_team,
        winner_confidence=confidence,
        winner_method=method,
    )


def _at(minutes):
    """A distinct game start time (distinct games have distinct times)."""
    return BASE + datetime.timedelta(minutes=minutes)


@pytest.fixture
def store(tmp_path):
    s = MatchStore(str(tmp_path / "test.db"))
    yield s
    s.close()


def test_ingest_and_roundtrip(store):
    original = _match()
    res = store.ingest(original, hash_replay(b"replay-bytes"), uploaded_by="tester")
    assert res.status == "inserted"
    assert store.get_match(res.match_id) == original


def test_exact_file_reupload_is_duplicate(store):
    h = hash_replay(b"same-bytes")
    assert store.ingest(_match(), h).status == "inserted"
    assert store.ingest(_match(file_name="other-name.SC2Replay"), h).status == "duplicate"
    assert store.match_count() == 1


def test_has_replay(store):
    h = hash_replay(b"abc")
    assert not store.has_replay(h)
    store.ingest(_match(), h)
    assert store.has_replay(h)


# --- cross-recording dedup (the same game recorded by different players) ---


def test_same_game_different_recording_dedups(store):
    # Player who left early: short, no winner recorded.
    early = _match(winning_team=None, confidence=0.0, method="unknown", duration=300, file_name="early.SC2Replay")
    # Teammate who stayed to the end: full game, winner recorded. Same game
    # (same start time + roster), different file bytes.
    full = _match(winning_team=1, confidence=1.0, method="recorded", duration=1200, file_name="full.SC2Replay")
    assert content_key(early) == content_key(full)

    r1 = store.ingest(early, hash_replay(b"early"))
    assert r1.status == "inserted"
    r2 = store.ingest(full, hash_replay(b"full"))
    assert r2.status == "updated"
    assert r2.match_id == r1.match_id

    assert store.match_count() == 1
    stored = store.get_match(r1.match_id)
    assert stored.winning_team == 1
    assert stored.winner_confidence == 1.0
    assert stored.duration_seconds == 1200


def test_worse_recording_is_ignored(store):
    full = _match(winning_team=1, confidence=1.0, duration=1200)
    early = _match(winning_team=None, confidence=0.0, method="unknown", duration=300)
    store.ingest(full, hash_replay(b"full"))
    r = store.ingest(early, hash_replay(b"early"))
    assert r.status == "duplicate"
    assert store.match_count() == 1
    assert store.get_match(r.match_id).winning_team == 1


def test_longer_recording_wins_at_equal_confidence(store):
    short = _match(winning_team=1, confidence=0.9, method="inferred:army", duration=600)
    long = _match(winning_team=1, confidence=0.9, method="inferred:army", duration=1400)
    store.ingest(short, hash_replay(b"short"))
    r = store.ingest(long, hash_replay(b"long"))
    assert r.status == "updated"
    assert store.get_match(r.match_id).duration_seconds == 1400


def test_confirmed_result_not_overwritten(store):
    uncertain = _match(winning_team=1, confidence=0.55, method="inferred:early-leaver", duration=600)
    r = store.ingest(uncertain, hash_replay(b"unc"))
    store.confirm_winner(r.match_id, 2)
    # A later full recording of the same game must not clobber the human call.
    full = _match(winning_team=1, confidence=1.0, method="recorded", duration=1400)
    r2 = store.ingest(full, hash_replay(b"full"))
    assert r2.status == "duplicate"
    stored = store.get_match(r.match_id)
    assert stored.winning_team == 2
    assert stored.winner_method == "confirmed"


def test_distinct_games_not_merged(store):
    # Same roster, different start times = different games.
    store.ingest(_match(played_at=_at(0)), hash_replay(b"g1"))
    store.ingest(_match(played_at=_at(20)), hash_replay(b"g2"))
    assert store.match_count() == 2


# --- winner confirmation & queries ---


def test_confirm_winner(store):
    r = store.ingest(_match(winning_team=None, confidence=0.0, method="unknown"), hash_replay(b"x"))
    pending = store.pending_confirmations(MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS)
    assert [mid for mid, _ in pending] == [r.match_id]

    store.confirm_winner(r.match_id, 2)
    loaded = store.get_match(r.match_id)
    assert loaded.winning_team == 2
    assert loaded.winner_confidence == 1.0
    assert loaded.winner_method == "confirmed"
    assert store.pending_confirmations(MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS) == []


def test_pending_excludes_short_and_confident(store):
    store.ingest(_match(winning_team=None, confidence=0.0, duration=60, played_at=_at(0)), hash_replay(b"short"))
    store.ingest(_match(played_at=_at(20)), hash_replay(b"decided"))
    store.ingest(
        _match(winning_team=1, confidence=0.55, method="inferred:early-leaver", played_at=_at(40)),
        hash_replay(b"weak"),
    )
    pending = store.pending_confirmations(MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS)
    assert len(pending) == 1
    assert pending[0][1].winner_confidence == 0.55


def test_all_matches_chronological(store):
    store.ingest(_match(played_at=_at(120)), hash_replay(b"later"))
    store.ingest(_match(played_at=_at(0)), hash_replay(b"earlier"))
    matches = [m for _, m in store.all_matches()]
    assert [m.played_at for m in matches] == [_at(0), _at(120)]


def test_unit_records(store):
    store.ingest(_match(winning_team=1, played_at=_at(0)), hash_replay(b"g1"))
    store.ingest(_match(winning_team=2, played_at=_at(20)), hash_replay(b"g2"))
    store.ingest(_match(winning_team=1, played_at=_at(40)), hash_replay(b"g3"))
    records = store.unit_records(MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS)
    # 4 players per pick per match: Zergling team won twice, lost once.
    assert records["Zergling"] == [8, 4]
    assert records["Marine"] == [4, 8]


def test_player_records_by(store):
    store.ingest(_match(winning_team=1, played_at=_at(0)), hash_replay(b"g1"))
    store.ingest(_match(winning_team=2, played_at=_at(20)), hash_replay(b"g2"))
    store.ingest(_match(winning_team=1, played_at=_at(40)), hash_replay(b"g3"))
    # A0 (h-A0) is team 1, Zerg/Zergling: won g1 & g3, lost g2.
    races = store.player_records_by("h-A0", "race", MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS)
    units = store.player_records_by("h-A0", "pick", MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS)
    assert races["Zerg"] == [2, 1]
    assert units["Zergling"] == [2, 1]
    with pytest.raises(ValueError):
        store.player_records_by("h-A0", "name; DROP TABLE matches", 0.7, 120)


def test_ratings_from_store(store):
    for i in range(5):
        store.ingest(_match(played_at=_at(i * 30)), hash_replay(f"game{i}".encode()))
    book = RatingBook.from_matches(m for _, m in store.all_matches())
    assert book.rated_matches == 5
    assert book.ratings["h-A0"].wins == 5
    assert book.ratings["h-A0"].ordinal > book.ratings["h-B0"].ordinal


def test_change_count_tracks_writes(store):
    assert store.change_count == 0
    r = store.ingest(_match(winning_team=None, confidence=0.0, method="unknown"), hash_replay(b"cc"))
    assert store.change_count == 1
    store.confirm_winner(r.match_id, 1)
    assert store.change_count == 2
    # exact re-upload is not a write
    store.ingest(_match(), hash_replay(b"cc"))
    assert store.change_count == 2


# -- schema migrations ---------------------------------------------------


class TestMigrations:
    def test_fresh_db_stamped_at_latest(self, tmp_path):
        import sqlite3

        from services.storage import SCHEMA_VERSION

        path = str(tmp_path / "fresh.db")
        MatchStore(path).close()
        version = sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0]
        assert version == SCHEMA_VERSION

    def test_preversioning_db_migrates_and_keeps_links(self, tmp_path):
        """A version-0 DB (no content_key column) with data and player links
        opens cleanly: content_key backfilled, links intact, version stamped."""
        import sqlite3

        from services.storage import SCHEMA_VERSION

        path = str(tmp_path / "old.db")
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE matches (
                id INTEGER PRIMARY KEY, file_hash TEXT NOT NULL UNIQUE,
                file_name TEXT NOT NULL, map_name TEXT NOT NULL,
                played_at TEXT NOT NULL, duration_seconds INTEGER NOT NULL,
                game_type TEXT NOT NULL, pick_mode TEXT NOT NULL,
                pick_phase_seconds INTEGER NOT NULL, winning_team INTEGER,
                winner_confidence REAL NOT NULL, winner_method TEXT NOT NULL,
                uploaded_by TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE match_players (
                match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
                name TEXT NOT NULL, toon_handle TEXT NOT NULL DEFAULT '',
                team INTEGER NOT NULL, race TEXT NOT NULL, pick TEXT,
                repick_used INTEGER, unit_counts TEXT NOT NULL
            );
            CREATE TABLE player_links (
                discord_id TEXT NOT NULL, sc2_name TEXT NOT NULL,
                toon_handle TEXT, linked_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (sc2_name)
            );
            """
        )
        conn.execute(
            """INSERT INTO matches (file_hash, file_name, map_name, played_at, duration_seconds,
               game_type, pick_mode, pick_phase_seconds, winning_team, winner_confidence, winner_method)
               VALUES ('abc', 'old.SC2Replay', 'Mono', ?, 900, '4v4', 'blind_random', 60, 1, 1.0, 'recorded')""",
            (BASE.isoformat(),),
        )
        conn.execute(
            "INSERT INTO match_players (match_id, name, toon_handle, team, race, pick, unit_counts)"
            " VALUES (1, 'A0', 'h-A0', 1, 'Zerg', 'Zergling', '{}')"
        )
        conn.execute("INSERT INTO player_links (discord_id, sc2_name, toon_handle) VALUES ('42', 'A0', 'h-A0')")
        conn.commit()
        conn.close()

        s = MatchStore(path)
        assert s.handles_for("42") == ["h-A0"]  # links survived
        assert s.match_count() == 1
        stored_key = s._conn.execute("SELECT content_key FROM matches WHERE id = 1").fetchone()[0]
        assert stored_key == content_key(_match(roster=["A0"]))
        assert s._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        s.close()
        MatchStore(path).close()  # reopening doesn't re-run migrations

    def test_newer_db_refused(self, tmp_path):
        import sqlite3

        path = str(tmp_path / "future.db")
        MatchStore(path).close()
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA user_version = 999")
        conn.commit()
        conn.close()
        with pytest.raises(RuntimeError, match="newer than this code"):
            MatchStore(path)


def test_replay_channel_toggle(store):
    assert store.replay_channel_ids() == set()
    assert store.toggle_replay_channel(123, 1, "42") is True
    assert store.replay_channel_ids() == {123}
    assert store.toggle_replay_channel(123, 1, "42") is False
    assert store.replay_channel_ids() == set()


def test_player_records_cover_merged_group(store):
    """Race/pick records must aggregate across a player's merged accounts."""
    store.ingest(_match(played_at=_at(0)), hash_replay(b"g1"))
    other = ["L0", "A1", "A2", "A3", "B0", "B1", "B2", "B3"]  # L0 = same person's alt
    store.ingest(_match(played_at=_at(10), roster=other), hash_replay(b"g2"))
    store.merge_accounts("h-A0", "h-L0")

    group = store.merged_handles("h-A0")
    assert set(group) == {"h-A0", "h-L0"}
    assert store.merged_handles("h-L0") == group  # same group from either end

    records = store.player_records_by(group, "pick", 0.7, 120)
    assert records["Zergling"] == [2, 0]  # one win from each account
    solo = store.player_records_by("h-A0", "pick", 0.7, 120)
    assert solo["Zergling"] == [1, 0]  # str form still means one account

    aliases = store.aliases_for_handles(group)
    assert aliases[0] == "L0"  # most recent name first
    assert "A0" in aliases


def test_legacy_config_key_folds_into_list():
    from models.config import BotConfig

    cfg = BotConfig.model_validate({"replays_channel_id": 111})
    assert cfg.replays_channel_ids == [111]
    cfg = BotConfig.model_validate({"replays_channel_id": 111, "replays_channel_ids": [222]})
    assert cfg.replays_channel_ids == [222, 111]
    assert BotConfig().replays_channel_ids == []


def test_h2h_records(store):
    """A0 vs B0 opposed in game 1 (A0's team won); teamed in game 2 (won)."""
    store.ingest(_match(played_at=_at(0)), hash_replay(b"h1"))  # A0 team1, B0 team2, team1 wins
    teamed = ["A0", "B0", "X", "Y", "C", "D", "E", "F"]  # both on team1
    store.ingest(_match(played_at=_at(10), roster=teamed), hash_replay(b"h2"))
    short = _match(played_at=_at(20), duration=60, winning_team=2)  # under duration gate
    store.ingest(short, hash_replay(b"h3"))

    vs, together, opposed = store.h2h_records(["h-A0"], ["h-B0"], 0.7, 120)
    assert vs == [1, 0]
    assert together == [1, 0]
    assert len(opposed) == 1
    match_id, match = opposed[0]
    assert {p.name for p in match.players} >= {"A0", "B0"}

    # perspective flips with argument order
    vs_flipped, _, _ = store.h2h_records(["h-B0"], ["h-A0"], 0.7, 120)
    assert vs_flipped == [0, 1]


def test_refresh_parse_updates_in_place(store):
    """Re-parsing the same file updates parsed fields but keeps a manually
    confirmed winner and doesn't duplicate the match."""
    first = _match(winning_team=None, confidence=0.0, method="unknown")
    result = store.ingest(first, hash_replay(b"r1"), uploaded_by="uploader")
    store.confirm_winner(result.match_id, 2)

    better = _match(winning_team=1, confidence=0.8, method="inferred:army")
    for p in better.players:
        p.repick_from = "SiegeTank" if p.repick_used else None
    assert store.refresh_parse(better, hash_replay(b"r1")) is True

    m = store.get_match(result.match_id)
    assert m.winning_team == 2  # confirmation preserved over the fresh parse
    assert m.winner_method == "confirmed"
    repicker = next(p for p in m.players if p.repick_used)
    assert repicker.repick_from == "SiegeTank"  # new parsed field landed
    assert store.match_count() == 1
    assert store.refresh_parse(better, hash_replay(b"unknown")) is False


def test_mvp_and_counts(store):
    """MVP = the player with the most enemy value destroyed, either team."""
    m = _match(played_at=_at(0))
    kills = {"A0": 5000, "A1": 9000, "A2": 100, "A3": 0, "B0": 12000, "B1": 0, "B2": 0, "B3": 0}
    for p in m.players:
        p.resources_killed = kills[p.name]
    store.ingest(m, hash_replay(b"m1"))

    _mid, stored = store.all_matches()[0]
    mvp = stored.mvp()
    assert mvp.name == "B0"  # top killer wins MVP even though team 2 lost
    assert store.mvp_count(["h-B0"], 0.7, 120) == 1
    assert store.mvp_count(["h-A1"], 0.7, 120) == 0

    # no kill stats (pre-archive parse) -> no MVP rather than an arbitrary one
    m2 = _match(played_at=_at(10))
    store.ingest(m2, hash_replay(b"m2"))
    _mid2, stored2 = store.all_matches()[1]
    assert stored2.mvp() is None


def test_awards_pick_outliers_only(store):
    """Awards go to statistical outliers within the game, capped at two."""
    from services.awards import match_awards

    m = _match(played_at=_at(0))
    for i, p in enumerate(m.players):
        p.resources_killed = 5000
        p.resources_lost = 9000  # everyone equal -> no Martyr despite the floor
        p.econ_killed = 500
        p.tech_killed = 400
        p.resources_floated = 900
    m.players[0].econ_killed = 6000  # clear Worker Slayer outlier
    m.players[1].resources_floated = 15000  # clear Banker outlier
    awards = match_awards(m)
    assert {a.key for a in awards} == {"worker_slayer", "banker"}
    assert {a.player.name for a in awards} == {"A0", "A1"}

    # outlier below the absolute floor -> no award
    m2 = _match(played_at=_at(10))
    for p in m2.players:
        p.econ_killed = 10
    m2.players[0].econ_killed = 800  # huge z, tiny value
    assert match_awards(m2) == []

    store.ingest(m, hash_replay(b"a1"))
    counts = store.award_counts(store.merged_handles("h-A0"), 0.7, 120)
    assert counts == {"worker_slayer": 1}
