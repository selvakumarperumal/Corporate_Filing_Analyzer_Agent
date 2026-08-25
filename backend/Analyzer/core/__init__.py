"""Cross-cutting basics: settings, paths, logging, token estimation.

Deliberately thin, and deliberately ignorant of the rest of the app. Nothing
here knows what a user or a filing is — the database lives in :mod:`db`, and
the domains own their own models, services and routes. That keeps the import
graph one-way: every package may import ``core``; ``core`` imports nobody.
"""

from core.config import settings
from core.logging import setup_logging
from core.paths import BACKEND_DIR, CHROMA_DIR, CONFIG_DIR, DATA_DIR, LOGS_DIR
from core.tokens import estimate_tokens

__all__ = [
    "BACKEND_DIR",
    "CHROMA_DIR",
    "CONFIG_DIR",
    "DATA_DIR",
    "LOGS_DIR",
    "estimate_tokens",
    "settings",
    "setup_logging",
]
