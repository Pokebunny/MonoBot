"""Zip uploads: the replays are read out defensively, previewed without
writing anything, and only stored once an admin adds them."""

import asyncio
import datetime as dt
import io
import zipfile
from types import SimpleNamespace

import pytest
from cogs import replays
from cogs.replays import Replays, classify_zip
from community import Community
from models.config import GuildConfig
from services import replay_zip
from services.storage import hash_replay
from test_storage import BASE, _match


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


# -- reading the zip ------------------------------------------------------


def test_takes_only_replays_from_anywhere_in_the_zip():
    data = _zip(
        {
            "a.SC2Replay": b"one",
            "season 3/b.sc2replay": b"two",
            "notes.txt": b"hi",
            "__MACOSX/season 3/._b.sc2replay": b"junk",
            "._a.SC2Replay": b"junk",
        }
    )
    assert replay_zip.read_replays(data) == [("a.SC2Replay", b"one"), ("b.sc2replay", b"two")]


@pytest.mark.parametrize(
    ("data", "why"),
    [
        (b"not a zip at all", "isn't a readable zip"),
        (_zip({"readme.txt": b"hi"}), "no .SC2Replay files"),
    ],
)
def test_refuses_what_it_cant_use(data, why):
    with pytest.raises(replay_zip.BadZip, match=why):
        replay_zip.read_replays(data)


def test_refuses_too_many_replays(monkeypatch):
    monkeypatch.setattr(replay_zip, "MAX_REPLAYS", 2)
    with pytest.raises(replay_zip.BadZip, match="limit is 2"):
        replay_zip.read_replays(_zip({f"{i}.SC2Replay": b"x" for i in range(3)}))


def test_judges_size_by_what_unpacks_not_what_the_zip_claims(monkeypatch):
    monkeypatch.setattr(replay_zip, "MAX_REPLAY_BYTES", 10)
    with pytest.raises(replay_zip.BadZip, match="too big"):
        replay_zip.read_replays(_zip({"big.SC2Replay": b"x" * 11}))
    monkeypatch.setattr(replay_zip, "MAX_REPLAY_BYTES", 100)
    monkeypatch.setattr(replay_zip, "MAX_TOTAL_BYTES", 15)
    with pytest.raises(replay_zip.BadZip, match="unpacks"):
        replay_zip.read_replays(_zip({"a.SC2Replay": b"x" * 10, "b.SC2Replay": b"y" * 10}))


# -- the preview ----------------------------------------------------------


def _later(minutes):
    return BASE + dt.timedelta(minutes=minutes)


def test_preview_says_what_adding_would_do_without_writing(store):
    stored = _match(confidence=0.6)
    store.ingest(stored, hash_replay(b"stored"))
    parsed = [
        ("same file.SC2Replay", b"stored", stored),
        ("better.SC2Replay", b"better", _match(confidence=1.0)),  # the stored game, recorded better
        ("new.SC2Replay", b"new", _match(played_at=_later(60), roster=[f"N{i}" for i in range(8)])),
        (
            "teammate.SC2Replay",  # another recording of that new game, in the same zip
            b"new-2",
            _match(played_at=_later(60), roster=[f"N{i}" for i in range(8)]),
        ),
        ("remake.SC2Replay", b"short", _match(played_at=_later(120), duration=60)),
        ("broken.SC2Replay", b"junk", None),
    ]
    outcomes = [e.outcome for e in classify_zip(store, parsed)]
    assert outcomes == ["stored", "better", "new", "in_zip", "rejected", "failed"]
    assert store.match_count() == 1  # nothing written


# -- adding ---------------------------------------------------------------


@pytest.fixture
def cog(store, tmp_path, monkeypatch):
    monkeypatch.setattr(replays, "REPLAY_ARCHIVE_DIR", str(tmp_path / "replays"))
    community = Community(None, GuildConfig(), store)
    client = SimpleNamespace(
        communities=SimpleNamespace(for_guild=lambda _guild: community, all=lambda: [community]),
        get_cog=lambda _name: None,
    )
    return Replays(client).at(community)


def test_add_stores_the_games_quietly_in_one_go(cog, store, tmp_path):
    new = [_match(played_at=_later(60 * i), roster=[f"P{i}{j}" for j in range(8)]) for i in range(3)]
    unsure = _match(played_at=_later(600), winning_team=None, confidence=0.0, roster=[f"U{j}" for j in range(8)])
    parsed = [(f"{i}.SC2Replay", f"game {i}".encode(), m) for i, m in enumerate([*new, unsure])]
    parsed.append(("remake.SC2Replay", b"short", _match(played_at=_later(900), duration=60)))
    entries = classify_zip(store, parsed)

    embed, _sweep = asyncio.run(cog.add_zip(entries, SimpleNamespace(id=7), "batch.zip"))

    assert store.match_count() == 4  # the remake stays out
    assert "Added **4 games**" in embed.description
    assert "1 game" in embed.fields[0].value  # the one with no winner, sent to !pending
    assert len(list((tmp_path / "replays").iterdir())) == 4  # each stored file archived once
    assert store.upload_count("7") == 4  # credited to whoever posted the zip
