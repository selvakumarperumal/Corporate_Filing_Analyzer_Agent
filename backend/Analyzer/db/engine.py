"""The Postgres engine, the session factory, and schema creation.

Tables are SQLModel classes, so one declaration is both the Pydantic model the
API validates against and the SQLAlchemy table it is stored in. They live with
the domain that owns them — :mod:`auth.models` for accounts,
:mod:`conversations.models` for the ledger — and this module knows nothing
about either beyond importing them once so ``create_all`` can see them.

Postgres is the only store. Everything else in the app is written for it: the
message metadata is ``jsonb``, the cascades are enforced by the server without
being asked, and the pool below is in front of a real one.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from core.config import settings

logger = logging.getLogger(__name__)

# Re-exported so the rest of the app has one place to import the declarative
# base from, whatever it happens to be underneath.
Base = SQLModel


engine: AsyncEngine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DB_ECHO,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    # A connection killed by something between us and Postgres — a restart, an
    # idle timeout on a proxy — looks alive in the pool until it is used. This
    # is the cheap round trip that finds out first.
    pool_pre_ping=True,
    pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
)


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
    # Imported for the side effect of registering the tables on SQLModel's
    # metadata — create_all only knows about classes that have been defined.
    from auth import models as _auth_models  # noqa: F401, PLC0415
    from conversations import models as _conversation_models  # noqa: F401, PLC0415

    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    logger.info("Database ready (%s)", safe_url())


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session that closes with the request."""
    async with SessionLocal() as session:
        yield session


def safe_url() -> str:
    """The database URL with any password stripped, for logging."""
    url = settings.DATABASE_URL
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host = rest.rpartition("@")
    return f"{scheme}://***@{host}"
