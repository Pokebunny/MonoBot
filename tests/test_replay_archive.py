"""The replay archive keeps one file per stored game: of two recordings of a
game, the weaker one's file is deleted -- unless another community stores it."""

import os
from types import SimpleNamespace

import pytest
from cogs import replays
from services.storage import MatchStore, hash_replay
from test_storage import _match


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setattr(replays, "REPLAY_ARCHIVE_DIR", str(tmp_path / "replays"))
    return tmp_path / "replays"


@pytest.fixture
def stores(tmp_path):
    home, other = MatchStore(str(tmp_path / "home.db")), MatchStore(str(tmp_path / "other.db"))
    yield home, other
    home.close()
    other.close()


def _client(*stores):
    communities = [SimpleNamespace(store=s) for s in stores]
    return SimpleNamespace(communities=SimpleNamespace(all=lambda: communities))


def _upload(client, store, data: bytes, match):
    result = store.ingest(match, hash_replay(data))
    replays.keep_archive(client, result, data, hash_replay(data))
    return result


def _files(archive):
    return set(os.listdir(archive)) if archive.exists() else set()


def test_better_recording_replaces_the_weaker_file(archive, stores):
    home, _ = stores
    client = _client(home)
    _upload(client, home, b"early", _match(confidence=0.0, winning_team=None, method="unknown", duration=300))
    assert _files(archive) == {f"{hash_replay(b'early')}.SC2Replay"}
    result = _upload(client, home, b"full", _match(duration=1200))
    assert (result.status, result.dropped_hash) == ("updated", hash_replay(b"early"))
    assert _files(archive) == {f"{hash_replay(b'full')}.SC2Replay"}


def test_weaker_upload_is_not_archived(archive, stores):
    home, _ = stores
    client = _client(home)
    _upload(client, home, b"full", _match(duration=1200))
    result = _upload(client, home, b"early", _match(duration=300))
    assert (result.status, result.dropped_hash) == ("duplicate", hash_replay(b"early"))
    assert _files(archive) == {f"{hash_replay(b'full')}.SC2Replay"}


def test_same_file_again_drops_nothing(archive, stores):
    home, _ = stores
    client = _client(home)
    _upload(client, home, b"full", _match())
    result = _upload(client, home, b"full", _match())
    assert (result.status, result.dropped_hash) == ("duplicate", None)
    assert _files(archive) == {f"{hash_replay(b'full')}.SC2Replay"}


def test_file_another_community_stores_is_kept(archive, stores):
    home, other = stores
    client = _client(home, other)
    _upload(client, other, b"early", _match(duration=300))
    _upload(client, home, b"early", _match(duration=300))
    _upload(client, home, b"full", _match(duration=1200))  # home drops "early"; other still holds it
    assert _files(archive) == {f"{hash_replay(b'early')}.SC2Replay", f"{hash_replay(b'full')}.SC2Replay"}
