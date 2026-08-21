"""Loads logging settings from ``config/logging.yaml``."""

from __future__ import annotations

import logging
import logging.config
from pathlib import Path

import yaml

# backend/Analyzer/core/logging_config.py -> backend/
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _BACKEND_DIR / "config" / "logging.yaml"
_LOG_DIR = _BACKEND_DIR / "logs"


def setup_logging() -> None:
    """Configure logging from the YAML file, falling back to a basic setup."""
    if not _CONFIG_PATH.exists():
        logging.basicConfig(level=logging.INFO)
        logging.warning("No logging config at %s — using defaults", _CONFIG_PATH)
        return

    config = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))

    # The YAML keeps file paths short and relative; make them absolute here so
    # logs always land in backend/logs/ no matter where the server was started.
    _LOG_DIR.mkdir(exist_ok=True)
    for handler in config.get("handlers", {}).values():
        if "filename" in handler:
            handler["filename"] = str(_BACKEND_DIR / handler["filename"])

    logging.config.dictConfig(config)
    logging.getLogger(__name__).debug("Logging configured from %s", _CONFIG_PATH)
