"""Logging configuration loader."""

from __future__ import annotations

import logging
import logging.config
from pathlib import Path

import yaml

_LOG_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "logging.yaml"


def setup_logging() -> None:
    """Configure logging from the YAML config file."""
    if _LOG_CONFIG_PATH.exists():
        with open(_LOG_CONFIG_PATH, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        logging.config.dictConfig(config)
    else:
        logging.basicConfig(level=logging.INFO)
        logging.warning("Logging config not found at %s, using basicConfig", _LOG_CONFIG_PATH)


def get_logger(name: str) -> logging.Logger:
    """Get a named logger."""
    return logging.getLogger(name)
