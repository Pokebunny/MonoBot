"""The desktop uploader (uploader/): which files it sends, and only once."""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "uploader"))

import monobot_uploader  # noqa: E402
from monobot_uploader import Uploader, replay_folders  # noqa: E402


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
