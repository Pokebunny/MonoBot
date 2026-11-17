from pydantic import BaseModel, model_validator


class GuildConfig(BaseModel):
    """Settings for one community (one Discord server's ladder)."""

    # Channels watched for replays and used for recaps/announcements. Empty =
    # every channel (noisy).
    replays_channel_ids: list[int] = []

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_single_channel(cls, data):
        """Fold in the legacy single replays_channel_id."""
        if isinstance(data, dict):
            legacy = data.pop("replays_channel_id", None)
            if legacy is not None:
                ids = list(data.get("replays_channel_ids", []))
                if legacy not in ids:
                    ids.append(legacy)
                data["replays_channel_ids"] = ids
        return data

    # "HH:MM" in an IANA timezone; None disables.
    queue_reset_time: str | None = "05:00"
    queue_reset_timezone: str = "America/New_York"

    # Daily recap of the previous 24 hours; None disables (!recap still works).
    recap_time: str | None = "08:00"
    recap_timezone: str = "America/New_York"

    # Minutes after their last Join press a queued player is dropped; None disables.
    queue_timeout_minutes: int | None = 60

    # In-game chat channel shown on the queue and proposals; None hides it.
    sc2_chat_channel: str | None = None
    sc2_region: str = "Americas"

    # Bot admins regardless of server permissions: top-level everywhere, a
    # guild's own list only there.
    admin_user_ids: list[int] = []

    # Guilds whose uploads (and winner confirmations) also count here. One way.
    counts_games_from: list[int] = []

    # Guilds an upload-API game is recorded in instead of here when at least
    # route_min_members of its players are linked to members of that guild
    # (not all 8: new people join before they link); the first that qualifies wins.
    route_member_games_to: list[int] = []
    route_min_members: int = 6


class UploadApiConfig(BaseModel):
    """The HTTP replay upload API (cogs/uploads.py, used by uploader/)."""

    port: int | None = None  # None: off
    # Loopback by default: a TLS proxy (deploy/README.md) faces the internet.
    host: str = "127.0.0.1"
    # Shown to players with their token, e.g. https://uploads.example.com
    public_url: str | None = None
    # The community uploads land in unless routed; None = home.
    default_guild_id: int | None = None


class BotConfig(GuildConfig):
    """Top-level fields are the home community's; `guilds` are the others."""

    home_guild_id: int | None = None  # None: home serves every unlisted server
    guilds: dict[int, GuildConfig] = {}
    upload_api: UploadApiConfig = UploadApiConfig()
