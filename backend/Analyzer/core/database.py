"""Database engine, session factory and schema creation.

Tables are SQLModel classes, so one declaration is both the Pydantic model the
API validates against and the SQLAlchemy table it is stored in — see
:mod:`models.user` for the accounts and :mod:`models.conversation` for the
message log.

The store is chosen entirely by ``DATABASE_URL``: SQLite by default so a fresh
checkout runs with nothing to install, any other SQLAlchemy async driver
(Postgres, MySQL) by setting that one variable.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from core.config import settings

logger = logging.getLogger(__name__)

# Re-exported so the rest of the app has one place to import the declarative
# base from, whatever it happens to be underneath.
Base = SQLModel


def _engine_kwargs() -> dict[str, object]:
    """Pool settings, which SQLite neither needs nor accepts."""
    if settings.DATABASE_URL.startswith("sqlite"):
        return {}
    return {
        "pool_size": settings.DB_POOL_SIZE,
        "max_overflow": settings.DB_MAX_OVERFLOW,
        "pool_pre_ping": True,
    }


engine: AsyncEngine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DB_ECHO,
    **_engine_kwargs(),
)


if settings.DATABASE_URL.startswith("sqlite"):

    @event.listens_for(engine.sync_engine, "connect")
    def _enforce_sqlite_foreign_keys(dbapi_connection, _record) -> None:
        """Turn on foreign key enforcement for every SQLite connection.

        SQLite ignores foreign keys unless asked not to, and the pragma is
        per-connection rather than a property of the file — so without this the
        ``ON DELETE CASCADE`` on ``refresh_tokens.user_id`` is decorative, and
        deleting an account would leave its tokens behind as orphans. Every
        other backend enforces constraints without being asked.
        """
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


# SQLModel's AsyncSession rather than SQLAlchemy's: it is the one that types
# `exec()` back to the model class, so a query returns User objects to the type
# checker and not bare Rows.
#
# expire_on_commit=False keeps a returned model readable after its session has
# committed, so a route can serialise the user it just created.
SessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    """Create the tables if they are not there yet.

    Enough for a single-service app; a deployment that needs versioned schema
    changes should put Alembic in front of this.
    """
    if settings.DATABASE_URL.startswith("sqlite"):
        # The file's directory has to exist before SQLite will open it, and a
        # fresh checkout has no data/ until something writes to it.
        path = settings.DATABASE_URL.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)

    # Imported for the side effect of registering the tables on SQLModel's
    # metadata — create_all only knows about classes that have been defined.
    from models import conversation as _conversation  # noqa: F401
    from models import user as _user  # noqa: F401

    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    logger.info("Database ready (%s)", _safe_url())


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session that closes with the request."""
    async with SessionLocal() as session:
        yield session


def _safe_url() -> str:
    """The database URL with any password stripped, for logging."""
    url = settings.DATABASE_URL
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host = rest.rpartition("@")
    return f"{scheme}://***@{host}"
