"""Per-deployment config from the gitignored config.json (defaults if missing).
Secrets live in .env."""

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
