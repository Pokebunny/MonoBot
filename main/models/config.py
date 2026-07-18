from pydantic import BaseModel, model_validator


class BotConfig(BaseModel):
    # Only parse .SC2Replay attachments posted in these channels. Empty means
    # watch every channel (simplest, but noisy — set this in production).
    replays_channel_ids: list[int] = []

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_single_channel(cls, data):
        """Older configs used a single replays_channel_id — fold it in."""
        if isinstance(data, dict):
            legacy = data.pop("replays_channel_id", None)
            if legacy is not None:
                ids = list(data.get("replays_channel_ids", []))
                if legacy not in ids:
                    ids.append(legacy)
                data["replays_channel_ids"] = ids
        return data

    # Role pinged when a matchmaking queue opens (e.g. a @monobattlers role).
    # None means no ping.
    queue_ping_role_id: int | None = None

    # Discord user IDs allowed to run bot-admin commands (merges, linking other
    # members, clearing the queue) regardless of server permissions.
    admin_user_ids: list[int] = []
