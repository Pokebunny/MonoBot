from pydantic import BaseModel, model_validator


class GuildConfig(BaseModel):
    """Settings for one community (one Discord server's ladder)."""

    # Only parse .SC2Replay attachments posted in these channels, which also
    # get the morning recap and achievement announcements. Empty means
    # watch every channel in the server (simplest, but noisy — set this in
    # production).
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

    # Daily session recap, posted to the replay channels, covering the
    # previous 24 hours. "HH:MM" in
    # recap_timezone; None disables the post (!recap still works on demand).
    recap_time: str | None = "08:00"
    recap_timezone: str = "America/New_York"

    # Queued players are removed this many minutes after they last pressed
    # Join (pressing it again while queued restarts the clock). None disables
    # the timeout.
    queue_timeout_minutes: int | None = 60

    # The in-game chat channel players meet in to start a game, and the
    # Battle.net region it's on. Shown on the queue and on every proposed
    # match; None leaves the hint off.
    sc2_chat_channel: str | None = None
    sc2_region: str = "Americas"

    # Discord user IDs allowed to run bot-admin commands (merges, linking other
    # members, clearing the queue) regardless of server permissions. The
    # top-level list is admin everywhere; a guild's own list only there.
    admin_user_ids: list[int] = []


class BotConfig(GuildConfig):
    """The top-level fields are the home community's settings — the original
    ladder, whose database is resources/monobot.db. Every server in `guilds`
    is a separate community with its own database, queue and settings."""

    # The server the home community belongs to. None (the legacy default)
    # means it serves every server not listed in `guilds`, as the bot did
    # before there was more than one community.
    home_guild_id: int | None = None

    # Other communities, by guild id.
    guilds: dict[int, GuildConfig] = {}
