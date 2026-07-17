"""Non-secret, per-deployment config loaded at import.

`config.json` (gitignored) holds deployment-specific values like the replays
channel ID. Secrets stay in `.env`; this file is for things safe to read but
specific to one server. Missing file -> permissive defaults so a fresh
checkout still boots.
"""

import logging
import os

from models.config import BotConfig

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


def load_config() -> BotConfig:
    if os.path.exists(_CONFIG_PATH):
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            return BotConfig.model_validate_json(f.read())
    logger.warning("No resources/config.json found; using defaults (watching every channel for replays)")
    return BotConfig()


CONFIG = load_config()
