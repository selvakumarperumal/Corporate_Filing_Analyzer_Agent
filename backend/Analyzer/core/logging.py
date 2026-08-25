"""Loads logging settings from ``config/logging.yaml``."""

from __future__ import annotations

import logging
import logging.config

import yaml

from core.paths import BACKEND_DIR, LOGGING_FILE, LOGS_DIR


def setup_logging() -> None:
    """Configure logging from the YAML file, falling back to a basic setup."""
    if not LOGGING_FILE.exists():
        logging.basicConfig(level=logging.INFO)
        logging.warning("No logging config at %s — using defaults", LOGGING_FILE)
        return

    config = yaml.safe_load(LOGGING_FILE.read_text(encoding="utf-8"))

    # The YAML keeps file paths short and relative; make them absolute here so
    # logs always land in backend/logs/ no matter where the server was started.
    LOGS_DIR.mkdir(exist_ok=True)
    for handler in config.get("handlers", {}).values():
        if "filename" in handler:
            handler["filename"] = str(BACKEND_DIR / handler["filename"])

    logging.config.dictConfig(config)
    logging.getLogger(__name__).debug("Logging configured from %s", LOGGING_FILE)
