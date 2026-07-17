import datetime

import pytest
from models.replay import MatchPlayer, MonobattleMatch
from services.rating import MIN_DURATION_SECONDS, MIN_WINNER_CONFIDENCE, RatingBook
from services.storage import MatchStore, hash_replay


def _match(
    winning_team=1,
    confidence=1.0,
    method="recorded",
    duration=900,
    played_at=None,
    file_name="test.SC2Replay",
):
    players = [
        MatchPlayer(
            name=f"{side}{i}",
            team=team,
            race="Zerg",
            pick="Zergling" if team == 1 else "Marine",
            repick_used=(i == 0),
            unit_counts={"Zergling": 100} if team == 1 else {"Marine": 80},
        )
        for team, side in ((1, "A"), (2, "B"))
        for i in range(4)
    ]
    return MonobattleMatch(
        file_name=file_name,
        map_name="Monobattle LotV - Map Rotation",
        played_at=played_at or datetime.datetime(2026, 7, 17, 12, 0, tzinfo=datetime.timezone.utc),
        duration_seconds=duration,
        game_type="4v4",
        pick_mode="blind_random",
        pick_phase_seconds=63,
        players=players,
        winning_team=winning_team,
        winner_confidence=confidence,
        winner_method=method,
    )


@pytest.fixture
def store(tmp_path):
    s = MatchStore(str(tmp_path / "test.db"))
    yield s
    s.close()


def test_ingest_and_roundtrip(store):
    original = _match()
    match_id = store.ingest(original, hash_replay(b"replay-bytes"), uploaded_by="tester")
    assert match_id is not None

    loaded = store.get_match(match_id)
    assert loaded == original


def test_duplicate_hash_rejected(store):
    h = hash_replay(b"same-bytes")
    assert store.ingest(_match(), h) is not None
    assert store.ingest(_match(file_name="other-name.SC2Replay"), h) is None
    assert store.match_count() == 1


def test_has_replay(store):
    h = hash_replay(b"abc")
    assert not store.has_replay(h)
    store.ingest(_match(), h)
    assert store.has_replay(h)


def test_confirm_winner(store):
    match_id = store.ingest(_match(winning_team=None, confidence=0.0, method="unknown"), hash_replay(b"x"))
    pending = store.pending_confirmations(MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS)
    assert [mid for mid, _ in pending] == [match_id]

    store.confirm_winner(match_id, 2)
    loaded = store.get_match(match_id)
    assert loaded.winning_team == 2
    assert loaded.winner_confidence == 1.0
    assert loaded.winner_method == "confirmed"
    assert store.pending_confirmations(MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS) == []


def test_pending_excludes_short_and_confident(store):
    store.ingest(_match(winning_team=None, confidence=0.0, duration=60), hash_replay(b"short"))
    store.ingest(_match(), hash_replay(b"decided"))
    store.ingest(_match(winning_team=1, confidence=0.55, method="inferred:early-leaver"), hash_replay(b"weak"))
    pending = store.pending_confirmations(MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS)
    assert len(pending) == 1
    assert pending[0][1].winner_confidence == 0.55


def test_all_matches_chronological(store):
    later = _match(played_at=datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc))
    earlier = _match(played_at=datetime.datetime(2026, 7, 16, tzinfo=datetime.timezone.utc))
    store.ingest(later, hash_replay(b"later"))
    store.ingest(earlier, hash_replay(b"earlier"))
    matches = [m for _, m in store.all_matches()]
    assert [m.played_at.day for m in matches] == [16, 18]


def test_unit_records(store):
    store.ingest(_match(winning_team=1), hash_replay(b"g1"))
    store.ingest(_match(winning_team=2), hash_replay(b"g2"))
    store.ingest(_match(winning_team=1), hash_replay(b"g3"))
    records = store.unit_records(MIN_WINNER_CONFIDENCE, MIN_DURATION_SECONDS)
    # 4 players per pick per match: Zergling team won twice, lost once.
    assert records["Zergling"] == [8, 4]
    assert records["Marine"] == [4, 8]


def test_ratings_from_store(store):
    for i in range(5):
        store.ingest(
            _match(played_at=datetime.datetime(2026, 7, 10 + i, tzinfo=datetime.timezone.utc)),
            hash_replay(f"game{i}".encode()),
        )
    book = RatingBook.from_matches(m for _, m in store.all_matches())
    assert book.rated_matches == 5
    assert book.ratings["A0"].wins == 5
    assert book.ratings["A0"].ordinal > book.ratings["B0"].ordinal


def test_change_count_tracks_writes(store):
    assert store.change_count == 0
    mid = store.ingest(_match(winning_team=None, confidence=0.0, method="unknown"), hash_replay(b"cc"))
    assert store.change_count == 1
    store.confirm_winner(mid, 1)
    assert store.change_count == 2
    # duplicate ingest is not a write
    store.ingest(_match(), hash_replay(b"cc"))
    assert store.change_count == 2
