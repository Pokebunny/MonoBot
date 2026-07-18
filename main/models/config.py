from pydantic import BaseModel


class BotConfig(BaseModel):
    # Only parse .SC2Replay attachments posted in this channel. None means
    # watch every channel (simplest, but noisy — set this in production).
    replays_channel_id: int | None = None

    # Role pinged when a matchmaking queue opens (e.g. a @monobattlers role).
    # None means no ping.
    queue_ping_role_id: int | None = None
