"""Advisory locks, for the housekeeping that must happen once and not N times.

Several instances of this app start at the same moment during a rolling
deploy, and each of them wants to create the schema, prune orphaned filings and
sweep interrupted runs. Done concurrently these range from wasteful to
genuinely broken — two processes running ``CREATE TABLE`` against the same
database is a race one of them loses, and losing it means the pod exits.

Postgres advisory locks are the right tool because Postgres is the one
dependency this app cannot run without. Redis is optional
(:mod:`core.leases`), so it cannot be where correctness lives.

Two shapes, and the difference matters:

:func:`held_for_transaction`
    blocking, released at commit. For work every instance must see finished
    before it carries on — schema creation.
:func:`only_one`
    non-blocking, released when the block exits. For work that only needs
    doing once, where the instances that lose should just skip it.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from db.engine import engine

logger = logging.getLogger(__name__)


def _key(name: str) -> int:
    """A stable signed 64-bit lock id for a name.

    Hashed with blake2b rather than :func:`hash`, whose seed is randomised per
    process — two instances must derive the same number from the same name or
    they are not taking the same lock at all.
    """
    digest = hashlib.blake2b(name.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


async def held_for_transaction(connection: AsyncConnection, name: str) -> None:
    """Take a named lock, held until the caller's transaction ends.

    Blocks until it is free. Whoever waits then sees the winner's committed
    work, which is the point: the second instance to reach ``create_all`` finds
    the tables already there rather than racing to make them.
    """
    await connection.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": _key(name)}
    )


@asynccontextmanager
async def only_one(name: str) -> AsyncIterator[bool]:
    """Hold a named lock for the block if it is free; yield whether it was.

    Never waits. A caller that is handed ``False`` should skip the work — some
    other instance is already doing it — not retry.

    The lock lives on its own connection for the length of the block, so the
    work inside is free to open and close database sessions of its own.
    """
    key = _key(name)
    connection = await engine.connect()
    acquired = False
    try:
        result = await connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
        )
        acquired = bool(result.scalar())
        yield acquired
    finally:
        try:
            if acquired:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                )
        except Exception:  # pragma: no cover - the lock dies with the session
            logger.debug("Releasing advisory lock %s failed", name, exc_info=True)
        await connection.close()
