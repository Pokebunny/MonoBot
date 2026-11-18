"""The desktop uploader (uploader/): which files it sends, and only once."""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "uploader"))

import monobot_uploader  # noqa: E402
from monobot_uploader import Uploader, peek, replay_folders, worth_sending  # noqa: E402

REAL_REPLAY = os.path.join(
    os.path.dirname(__file__), "..", "test_replays", "Monobattle LotV - Map Rotation (713).SC2Replay"
)


def _folder(tmp_path):
    folder = tmp_path / "Accounts" / "123" / "1-S2-1-456" / "Replays" / "Multiplayer"
    folder.mkdir(parents=True)
    return folder


def _write(folder, name, data=b"replay", age=60):
    path = folder / name
    path.write_bytes(data)
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def _uploader(tmp_path, folder, since):
    sent = []
    config = {"server_url": "https://example.invalid", "token": "t", "folders": [str(folder)], "since": since}

    def post(_server, _token, name, _data):
        sent.append(name)
        return {"status": "stored", "server": "Mono", "match": len(sent)}

    uploader = Uploader(config, str(tmp_path / "config.json"), post=post)
    return uploader, sent


def test_peek_reads_the_map_and_teams_without_sc2reader():
    with open(REAL_REPLAY, "rb") as f:
        assert peek(f.read()) == ("Monobattle LotV - Map Rotation", [4, 4], False)


def test_only_what_could_be_a_monobattle_is_sent(monkeypatch):
    for title, teams, blizzard_map, marked, send in [
        ("Monobattle LotV - Map Rotation", [4, 4], False, False, True),
        # An Editor test of the mod clears the Blizzard-map flag: the server decides.
        ("Brass Devil", [4, 4], False, False, True),
        ("Fortitude LE", [4, 4], True, True, True),  # a lobby game with the mod keeps it
        ("Brass Devil", [4, 4], True, False, False),  # an ordinary 4v4 on it
        ("Scythe of Amon", [4, 4], True, False, False),  # co-op, AI included
        ("Ulrena LE", [1, 1], True, True, False),
        ("Dead of Night", [2], True, False, False),
    ]:
        result = monobot_uploader.Peek(title, teams, blizzard_map)
        monkeypatch.setattr(monobot_uploader, "peek", lambda _data, r=result: r)
        monkeypatch.setattr(monobot_uploader, "has_mod_markers", lambda _data, m=marked: m)
        assert worth_sending(b"")[0] is send, result


def test_mod_markers_are_found_in_the_tracker_events():
    with open(REAL_REPLAY, "rb") as f:
        assert not monobot_uploader.has_mod_markers(f.read())


def test_a_replay_it_cannot_read_is_sent_anyway():
    assert worth_sending(b"not a replay") == (True, "")


def test_a_skipped_replay_is_not_read_again(tmp_path, monkeypatch):
    folder = _folder(tmp_path)
    _write(folder, "Ulrena LE.SC2Replay")
    monkeypatch.setattr(monobot_uploader, "peek", lambda _data: monobot_uploader.Peek("Ulrena LE", [1, 1], True))
    uploader, sent = _uploader(tmp_path, folder, since=0)
    uploader.scan()
    uploader.scan()
    assert sent == []
    monkeypatch.setattr(monobot_uploader, "peek", lambda _data: (_ for _ in ()).throw(AssertionError("re-read")))
    uploader.scan()


def test_finds_every_accounts_multiplayer_folder(tmp_path):
    folder = _folder(tmp_path)
    assert replay_folders([str(tmp_path / "Accounts")]) == [str(folder)]


def test_sends_every_new_replay_once(tmp_path):
    folder = _folder(tmp_path)
    _write(folder, "Monobattle LotV - Map Rotation (3).SC2Replay", age=120)
    # A MonoBattle mod game is named after its melee map: the server decides.
    _write(folder, "Ulrena LE.SC2Replay", b"mod game", age=60)
    _write(folder, "Monobattle LotV - Map Rotation (2).SC2Replay", b"old", age=3600)
    uploader, sent = _uploader(tmp_path, folder, since=time.time() - 600)
    uploader.scan()  # first look: sizes not settled yet
    assert sent == []
    uploader.scan()
    assert sent == ["Monobattle LotV - Map Rotation (3).SC2Replay", "Ulrena LE.SC2Replay"]
    uploader.scan()
    assert len(sent) == 2


def test_a_file_still_being_written_waits(tmp_path, monkeypatch):
    folder = _folder(tmp_path)
    path = _write(folder, "Monobattle (1).SC2Replay", b"part")
    uploader, sent = _uploader(tmp_path, folder, since=0)
    uploader.scan()
    path.write_bytes(b"partial and growing")
    os.utime(path, (time.time() - 60,) * 2)
    uploader.scan()
    assert sent == []


def test_remembers_uploads_across_restarts(tmp_path):
    folder = _folder(tmp_path)
    _write(folder, "Monobattle (1).SC2Replay")
    uploader, sent = _uploader(tmp_path, folder, since=0)
    uploader.scan()
    uploader.scan()
    assert len(sent) == 1
    config = monobot_uploader.load_json(str(tmp_path / "config.json"))
    again, sent_again = _uploader(tmp_path, folder, since=0)
    again.uploaded = set(config["uploaded"])
    again.scan()
    again.scan()
    assert sent_again == []
