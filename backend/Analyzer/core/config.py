"""Application configuration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode

# backend/Analyzer/core/config.py -> backend/
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_DB_PATH = _BACKEND_DIR / "data" / "app.db"


class Settings(BaseSettings):
    """App settings loaded from environment / .env file."""

    OLLAMA_MODEL: str = "llama3.1:latest"
    OLLAMA_EMBEDDING_MODEL: str = "nomic-embed-text:latest"
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    CHROMA_COLLECTION: str = "corporate_filings"

    # ── Database ─────────────────────────────────────────────────────────
    # Any async SQLAlchemy URL, which is what SQLModel takes too. SQLite needs
    # no server and is the default; point DATABASE_URL at Postgres
    # (postgresql+asyncpg://user:pass@host/db) to move the accounts off the
    # local file without touching the code.
    DATABASE_URL: str = f"sqlite+aiosqlite:///{_DEFAULT_DB_PATH}"
    DB_ECHO: bool = False
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10

    # ── Conversation history ─────────────────────────────────────────────
    # What the model is sent is not what the analyst sees: the ledger keeps
    # every message, while a run carries only the last few turns plus a rolling
    # summary of what came before. These bound that second, smaller history.
    HISTORY_CONTEXT_MESSAGES: int = 10
    HISTORY_CONTEXT_TOKENS: int = 1500
    # Unsummarised messages tolerated before the older ones are folded into the
    # conversation's rolling summary.
    HISTORY_SUMMARY_THRESHOLD: int = 24
    HISTORY_PAGE_SIZE: int = 50
    HISTORY_MAX_PAGE_SIZE: int = 200

    # ── Redis (optional) ─────────────────────────────────────────────────
    # A read cache in front of the message table, holding each conversation's
    # hot tail. Leave REDIS_URL blank and the cache is simply off — every read
    # goes to the database, which is correct, just slower.
    REDIS_URL: str = ""
    REDIS_KEY_PREFIX: str = "cfa"
    REDIS_HOT_WINDOW: int = 40
    REDIS_TTL_SECONDS: int = 3600

    # ── Auth ─────────────────────────────────────────────────────────────
    # JWT_SECRET_KEY signs both token kinds. Leave it unset only in local
    # development — the app refuses to start without one anywhere else,
    # because a guessable secret means anyone can mint a valid access token.
    JWT_SECRET_KEY: str = ""
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14
    # Where the browser is served from, for the CORS allow-list. Credentials
    # ride in the Authorization header, so the wildcard still works, but a
    # deployment should name its origins here.
    # NoDecode: pydantic-settings would otherwise JSON-decode a list field
    # straight out of the environment, before any validator of ours runs.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = ["*"]

    model_config = {"env_file": ".env", "extra": "ignore"}

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept a comma-separated list, not just JSON.

        pydantic-settings parses a ``list[str]`` field from the environment as
        JSON, so ``CORS_ORIGINS=http://localhost:8080`` raised at startup and
        only ``["http://localhost:8080"]`` worked. Every other way of setting
        an env var — a shell, a Dockerfile, compose, a k8s manifest — writes
        the plain comma-separated form, so that is what this accepts.
        """
        if not isinstance(value, str):
            return value
        text = value.strip()
        if text.startswith("["):
            return json.loads(text)
        return [part.strip() for part in text.split(",") if part.strip()]

    @model_validator(mode="after")
    def _widen_hot_window(self) -> Settings:
        """Keep the cached tail at least as long as the window a run reads.

        A cache holding fewer messages than a run asks for answers every
        request as a miss, which is worse than not caching at all: the round
        trip to Redis is paid and the database is read anyway.
        """
        if self.REDIS_HOT_WINDOW < self.HISTORY_CONTEXT_MESSAGES:
            self.REDIS_HOT_WINDOW = self.HISTORY_CONTEXT_MESSAGES
        return self


settings = Settings()
