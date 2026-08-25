"""Where the app's files live on disk.

Every runtime path is resolved from this one module rather than by counting
``parents[...]`` in whichever file happens to need it. That counting is fragile
in exactly the way a reorganisation exposes: a module moved one directory
deeper silently starts pointing at the wrong tree. Here it is stated once.

The layout these describe is the one the Dockerfile also builds::

    backend/
    ├── Analyzer/   this package
    ├── config/     prompts.yaml, logging.yaml
    ├── data/       the Chroma vector store
    └── logs/       rotated log files
"""

from __future__ import annotations

from pathlib import Path

# core/paths.py -> core -> Analyzer -> backend
BACKEND_DIR = Path(__file__).resolve().parents[2]

CONFIG_DIR = BACKEND_DIR / "config"
DATA_DIR = BACKEND_DIR / "data"
LOGS_DIR = BACKEND_DIR / "logs"

PROMPTS_FILE = CONFIG_DIR / "prompts.yaml"
LOGGING_FILE = CONFIG_DIR / "logging.yaml"

# The vector store. Filings are the one thing the app keeps on its own disk;
# accounts and messages are in Postgres.
CHROMA_DIR = DATA_DIR / "chroma_db"

__all__ = [
    "BACKEND_DIR",
    "CHROMA_DIR",
    "CONFIG_DIR",
    "DATA_DIR",
    "LOGGING_FILE",
    "LOGS_DIR",
    "PROMPTS_FILE",
]
