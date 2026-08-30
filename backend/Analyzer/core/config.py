"""Application configuration.

One settings object, read from the environment (or a ``.env`` beside the
process). Nothing else in the app reads ``os.environ`` — a setting that is not
declared here does not exist.
"""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode

# Drivers that speak the async protocol every query in this app is awaited on.
# A synchronous URL (``postgresql://``) builds an engine that raises on first
# use, which is a confusing way to find out at request time what can be said
# plainly at startup.
_ASYNC_POSTGRES_SCHEMES = ("postgresql+asyncpg://", "postgresql+psycopg://")


class Settings(BaseSettings):
    """App settings loaded from environment / .env file."""

    OLLAMA_MODEL: str = "llama3.1:latest"
    OLLAMA_EMBEDDING_MODEL: str = "nomic-embed-text:latest"
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    CHROMA_COLLECTION: str = "corporate_filings"

    # ── Vector store ─────────────────────────────────────────────────────
    # Blank means embedded: a Chroma directory on this process's own disk,
    # which is correct for one instance and wrong for two. An embedded store
    # is a library reading a local path, so a second process gets a second,
    # private copy — a filing uploaded through one is invisible to the other,
    # and pointing both at one shared volume corrupts the SQLite index instead.
    #
    # Set CHROMA_HOST and every instance talks to one store over HTTP, which
    # is what makes the API stateless and safe to replicate.
    CHROMA_HOST: str = ""
    CHROMA_PORT: int = 8000
    CHROMA_SSL: bool = False

    # ── Database ─────────────────────────────────────────────────────────
    # Postgres, always. The schema leans on it — ``jsonb`` for message
    # metadata, foreign keys that are enforced without being asked, a pool in
    # front of a real server — and the deployment (compose, and the CNPG
    # cluster in deploy/) runs it. The default points at a local server; a
    # deployment overrides it.
    DATABASE_URL: str = (
        "postgresql+asyncpg://analyzer:analyzer@localhost:5432/filing_analyzer"
    )
    DB_ECHO: bool = False
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_RECYCLE_SECONDS: int = 1800

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

    # ── Running more than one instance ───────────────────────────────────
    # A Socket.IO client manager, so an ``emit`` from one instance can reach a
    # connection held by another. Not needed today — every event this app
    # sends goes to the asker, from the very process holding their connection
    # — and it costs a Redis round trip per emit, so it stays off until the
    # first broadcast (a shared dossier, the same analyst on two devices)
    # makes it necessary. ``redis://…`` turns it on.
    SOCKETIO_MESSAGE_QUEUE_URL: str = ""

    # How long shutdown waits for answers that are still streaming before it
    # gives up on them. A run killed halfway leaves a question in the ledger
    # with no answer beside it, so this wants to be longer than a slow answer
    # — and shorter than the platform's own patience (Kubernetes'
    # terminationGracePeriodSeconds, which defaults to a useless 30).
    SHUTDOWN_DRAIN_SECONDS: int = 120

    # A question this old with no answer after it was interrupted by something
    # that did not come back — a SIGKILL, a lost node. Swept at startup and
    # marked failed, so the dossier does not show a question that will never
    # be answered. Generous on purpose: it must never overtake a run that is
    # merely slow, on another instance, right now.
    STALE_RUN_MINUTES: int = 30

    # ── Auth ─────────────────────────────────────────────────────────────
    # JWT_SECRET_KEY signs both token kinds. Leave it unset only in local
    # development — the app then signs with a key that dies with the process,
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

    @field_validator("DATABASE_URL")
    @classmethod
    def _require_async_postgres(cls, value: str) -> str:
        """Refuse at startup what would otherwise fail at the first query."""
        url = value.strip()
        if not url.startswith(_ASYNC_POSTGRES_SCHEMES):
            raise ValueError(
                "DATABASE_URL must be an async Postgres URL, e.g. "
                "postgresql+asyncpg://user:password@host:5432/filing_analyzer "
                f"(got {url.split('://', 1)[0] or url!r})"
            )
        return url

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
