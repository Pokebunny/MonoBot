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


def test_ratings_from_store(store):
    for i in range(5):
        store.ingest(_match(played_at=_at(i * 30)), hash_replay(f"game{i}".encode()))
    book = RatingBook.from_matches(m for _, m in store.all_matches())
    assert book.rated_matches == 5
    assert book.ratings["A0"].wins == 5
    assert book.ratings["A0"].ordinal > book.ratings["B0"].ordinal


def test_change_count_tracks_writes(store):
    assert store.change_count == 0
    r = store.ingest(_match(winning_team=None, confidence=0.0, method="unknown"), hash_replay(b"cc"))
    assert store.change_count == 1
    store.confirm_winner(r.match_id, 1)
    assert store.change_count == 2
    # exact re-upload is not a write
    store.ingest(_match(), hash_replay(b"cc"))
    assert store.change_count == 2
