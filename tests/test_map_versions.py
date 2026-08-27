"""The real map behind a monobattle: reading it out of a published arcade
map, caching it per version, and labelling matches with it."""

import asyncio
import datetime
import sqlite3

import pytest
from models.replay import MapVersion, MatchPlayer, MonobattleMatch
from services import map_versions
from services.replay_parser import _parse_current_map
from services.storage import MatchStore, hash_replay

BASE = datetime.datetime(2026, 8, 26, 4, 28, tzinfo=datetime.timezone.utc)

ASHEN = "bc8ffe0997bdbd0a1214cc4eaae3cdc75e738a4a72a5a02dcfa79c99b47cfcd1"
FORTITUDE = "d47b4f1ab2a6" + "0" * 52


def _match(map_hash="", played_at=None, file_name="test.SC2Replay"):
    players = [
        MatchPlayer(
            name=f"P{i}",
            toon_handle=f"h-{i}",
            team=(1 if i < 4 else 2),
            race="Zerg",
            pick="Zergling",
            unit_counts={"Zergling": 50},
        )
        for i in range(8)
    ]
    return MonobattleMatch(
        file_name=file_name,
        map_name="Monobattle LotV - Map Rotation",
        map_hash=map_hash,
        played_at=played_at or BASE,
        duration_seconds=900,
        game_type="4v4",
        pick_mode="blind_random",
        pick_phase_seconds=63,
        players=players,
        winning_team=1,
        winner_confidence=1.0,
        winner_method="recorded",
    )


@pytest.fixture
def store(tmp_path):
    s = MatchStore(str(tmp_path / "test.db"))
    yield s
    s.close()


class TestParseHeader:
    """Real header lines, as seen across every published version in the
    archive since 2023 — the formatting is prose and drifts."""

    def test_tagged_author(self):
        header = (
            b'Current Map: The Ashen Cradle <c val="80FFFF">(Created by KillerSmile)</c>'
            b'<n/>Join our Discord: <c val="80FFFF">https://discord.gg/G4fpzgQ</c>\x15'
        )
        assert _parse_current_map(header) == ("The Ashen Cradle", "KillerSmile")

    def test_plain_author(self):
        assert _parse_current_map(b"Current Map: Fortitude (Created by Superouman)\x15\x00") == (
            "Fortitude",
            "Superouman",
        )
        assert _parse_current_map(b"Current Map: Concord (made by me)\x15\x00") == ("Concord", "me")

    def test_no_author(self):
        assert _parse_current_map(b"Current Map: Enigma\x15\x00") == ("Enigma", None)

    def test_description_prose_is_not_part_of_the_name(self):
        header = b"Current Map: The Ashen Cradle<n/><n/>Join us on discord: https://discord.gg/x\x15\x00MapInfo"
        assert _parse_current_map(header) == ("The Ashen Cradle", None)

    def test_credited_copy_wins(self):
        """The header names the map twice; only one copy credits its author."""
        header = (
            b"Current Map: The Ashen Cradle<n/><n/>Join us on discord: https://discord.gg/x\x15\x00"
            b'Current Map: The Ashen Cradle <c val="80FFFF">(Created by KillerSmile)</c>\x15'
        )
        assert _parse_current_map(header) == ("The Ashen Cradle", "KillerSmile")

    def test_header_that_names_no_map(self):
        assert _parse_current_map(b"DocInfo/NameSUne Monobattle LotV - Map Rotation\x00") is None


class TestCache:
    def test_hash_stored_with_the_match(self, store):
        store.ingest(_match(map_hash=ASHEN), hash_replay(b"a"))
        assert store.get_match(1).map_hash == ASHEN

    def test_unresolved_then_resolved(self, store):
        store.ingest(_match(map_hash=ASHEN), hash_replay(b"a"))
        assert store.unresolved_map_hashes() == [ASHEN]
        store.record_map_version(ASHEN, MapVersion(map_hash=ASHEN, name="The Ashen Cradle", author="KillerSmile"))
        assert store.unresolved_map_hashes() == []
        assert store.map_version_names() == {ASHEN: "The Ashen Cradle"}

    def test_failed_lookup_is_not_retried(self, store):
        """A version whose map can't be read caches as nameless, so uploads
        don't hit the depot again for it every time."""
        store.ingest(_match(map_hash=ASHEN), hash_replay(b"a"))
        store.record_map_version(ASHEN, None)
        assert store.unresolved_map_hashes() == []
        assert store.map_version_names() == {}

    def test_matches_without_a_hash_are_not_pending(self, store):
        store.ingest(_match(), hash_replay(b"a"))
        assert store.unresolved_map_hashes() == []

    def test_samples_are_a_timeline(self, store):
        store.ingest(_match(map_hash=FORTITUDE, played_at=BASE - datetime.timedelta(days=2)), hash_replay(b"a"))
        store.ingest(_match(map_hash=ASHEN, file_name="b.SC2Replay"), hash_replay(b"b"))
        assert [h for _, h in store.map_hash_samples()] == [FORTITUDE, ASHEN]


class TestResolvePending:
    def test_resolves_each_version_once(self, store, monkeypatch):
        store.ingest(_match(map_hash=ASHEN), hash_replay(b"a"))
        store.ingest(
            _match(map_hash=ASHEN, file_name="b.SC2Replay", played_at=BASE + datetime.timedelta(hours=1)),
            hash_replay(b"b"),
        )
        calls = []

        def fake_fetch(map_hash):
            calls.append(map_hash)
            return MapVersion(map_hash=map_hash, name="The Ashen Cradle", author="KillerSmile")

        monkeypatch.setattr(map_versions.replay_parser, "fetch_map_version", fake_fetch)
        resolved = map_versions.resolve_pending(store)
        assert [v.name for v in resolved] == ["The Ashen Cradle"]
        assert calls == [ASHEN]  # two games, one version, one download
        assert map_versions.resolve_pending(store) == []  # cached

    def test_unreachable_depot_leaves_the_arcade_name(self, store, monkeypatch):
        match = _match(map_hash=ASHEN)
        store.ingest(match, hash_replay(b"a"))
        monkeypatch.setattr(map_versions.replay_parser, "fetch_map_version", lambda h: None)
        assert map_versions.resolve_pending(store) == []
        assert map_versions.label(match, store.map_version_names()) == "Monobattle LotV - Map Rotation"


class TestResolveFromTheBot:
    """The bot resolves map versions while handling an upload. The store's
    sqlite connection belongs to the thread that created it, so the DB half
    of the work has to stay on the event-loop thread."""

    def test_async_resolver_records_versions(self, store, monkeypatch):
        store.ingest(_match(map_hash=ASHEN), hash_replay(b"a"))
        monkeypatch.setattr(
            map_versions.replay_parser,
            "fetch_map_version",
            lambda h: MapVersion(map_hash=h, name="The Ashen Cradle", author="KillerSmile"),
        )
        resolved = asyncio.run(map_versions.resolve_pending_async(store))
        assert [v.name for v in resolved] == ["The Ashen Cradle"]
        assert store.map_version_names() == {ASHEN: "The Ashen Cradle"}

    def test_async_resolver_caches_a_failed_lookup(self, store, monkeypatch):
        store.ingest(_match(map_hash=ASHEN), hash_replay(b"a"))
        monkeypatch.setattr(map_versions.replay_parser, "fetch_map_version", lambda h: None)
        assert asyncio.run(map_versions.resolve_pending_async(store)) == []
        assert store.unresolved_map_hashes() == []

    def test_blocking_resolver_cannot_be_thrown_at_a_worker_thread(self, store):
        """Why resolve_pending_async exists: handing the whole store to
        asyncio.to_thread raises, which once took the match summary down with
        it (the upload posted nothing at all)."""
        store.ingest(_match(map_hash=ASHEN), hash_replay(b"a"))

        async def off_thread():
            await asyncio.to_thread(map_versions.resolve_pending, store)

        with pytest.raises(sqlite3.ProgrammingError):
            asyncio.run(off_thread())


class TestGrouping:
    """Per-map stats group on the map's name, never its hash: the author
    republishes the same terrain under a new hash from time to time."""

    def test_two_versions_of_one_map_are_one_group(self):
        names = {ASHEN: "The Ashen Cradle", FORTITUDE: "The Ashen Cradle"}
        matches = [_match(map_hash=ASHEN), _match(map_hash=FORTITUDE)]
        groups = map_versions.group_by_map(matches, names)
        assert list(groups) == ["The Ashen Cradle"]
        assert len(groups["The Ashen Cradle"]) == 2

    def test_distinct_maps_stay_apart_newest_first(self):
        names = {ASHEN: "The Ashen Cradle", FORTITUDE: "Fortitude"}
        old = _match(map_hash=FORTITUDE, played_at=BASE - datetime.timedelta(days=30))
        groups = map_versions.group_by_map([old, _match(map_hash=ASHEN)], names)
        assert list(groups) == ["The Ashen Cradle", "Fortitude"]

    def test_unidentified_games_are_left_out(self):
        """A game with no hash, or one whose version never resolved, has no
        map — the arcade name must not become a bucket of its own."""
        matches = [_match(), _match(map_hash=FORTITUDE), _match(map_hash=ASHEN)]
        groups = map_versions.group_by_map(matches, {ASHEN: "The Ashen Cradle"})
        assert list(groups) == ["The Ashen Cradle"]
        assert len(matches) - sum(len(v) for v in groups.values()) == 2

    def test_named_is_none_when_unknown(self):
        assert map_versions.named(_match(map_hash=ASHEN), {}) is None
        assert map_versions.named(_match(), {ASHEN: "x"}) is None
        assert map_versions.named(_match(map_hash=ASHEN), {ASHEN: "x"}) == "x"


class TestLabel:
    def test_known_version(self):
        assert map_versions.label(_match(map_hash=ASHEN), {ASHEN: "The Ashen Cradle"}) == "The Ashen Cradle"

    def test_unknown_version_falls_back(self):
        assert map_versions.label(_match(map_hash=ASHEN), {}) == "Monobattle LotV - Map Rotation"

    def test_match_stored_before_hashes_existed(self):
        assert map_versions.label(_match(), {ASHEN: "The Ashen Cradle"}) == "Monobattle LotV - Map Rotation"
