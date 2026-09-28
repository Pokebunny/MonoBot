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

    # Daily automatic queue reset, for players who joined, never got a game,
    # and forgot to leave. "HH:MM" in queue_reset_timezone (an IANA name);
    # None disables the reset.
    queue_reset_time: str | None = "05:00"
    queue_reset_timezone: str = "America/New_York"

    # AFK check: after this many minutes in the queue without a game, a player
    # is asked to confirm they're still there, and is removed if they don't
    # within afk_check_grace_minutes. Confirming restarts the clock. None
    # disables the check.
    afk_check_minutes: int | None = 30
    afk_check_grace_minutes: int = 5

    # Discord user IDs allowed to run bot-admin commands (merges, linking other
    # members, clearing the queue) regardless of server permissions.
    admin_user_ids: list[int] = []
