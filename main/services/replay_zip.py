"""Read replays out of an untrusted .zip: in memory only, .SC2Replay members
only, with size limits far above a real upload to stop zip bombs."""

import io
import os
import zipfile

MAX_REPLAYS = 300
# A 4v4 monobattle replay is ~0.2-1 MB; nothing real comes near this.
MAX_REPLAY_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 300 * 1024 * 1024


class BadZip(Exception):
    """The upload can't be used; the message says why, for the channel."""


def is_zip(file_name: str) -> bool:
    return file_name.lower().endswith(".zip")


def read_replays(data: bytes) -> list[tuple[str, bytes]]:
    """(file name, bytes) for each replay in the zip, in the zip's order.
    Folders inside the zip are fine; the name kept is the file's own."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise BadZip("it isn't a readable zip file") from e
    with archive:
        members = [
            info
            for info in archive.infolist()
            if not info.is_dir()
            and info.filename.lower().endswith(".sc2replay")
            # macOS zips carry "._name" metadata twins of every file.
            and not os.path.basename(info.filename).startswith("._")
            and not info.filename.startswith("__MACOSX/")
        ]
        if not members:
            raise BadZip("there are no .SC2Replay files in it")
        if len(members) > MAX_REPLAYS:
            raise BadZip(f"it holds {len(members)} replays; the limit is {MAX_REPLAYS} per zip")
        replays, total = [], 0
        for info in members:
            name = os.path.basename(info.filename)
            # The sizes in the zip's directory can lie, so read at most one
            # byte past the limit and judge by what actually came out.
            try:
                with archive.open(info) as f:
                    content = f.read(MAX_REPLAY_BYTES + 1)
            except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as e:
                # RuntimeError: encrypted; NotImplementedError: unusual compression.
                raise BadZip(f"`{name}` couldn't be read from it ({e})") from e
            if len(content) > MAX_REPLAY_BYTES:
                raise BadZip(f"`{name}` is far too big to be a replay")
            total += len(content)
            if total > MAX_TOTAL_BYTES:
                raise BadZip("it unpacks to far more than any set of replays would")
            replays.append((name, content))
        return replays
