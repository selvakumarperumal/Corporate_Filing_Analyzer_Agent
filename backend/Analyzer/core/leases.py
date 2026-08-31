"""Short-lived exclusive claims across instances, when Redis is there to help.

One job in this app is worth not doing twice but survivable if it is: folding a
conversation's older turns into its rolling summary. Two instances that both
decide to fold the same dossier write the same three columns and the last write
wins — correct, and a wasted call to a model that is already the bottleneck.

So this is deliberately *best effort*. With Redis it is a real cross-instance
lease. Without it — ``REDIS_URL`` unset, or Redis down — :meth:`acquire`
answers yes to everybody and the caller falls back to whatever it does within
one process. Nothing here may fail a request, and nothing that must be correct
may be built on it; that is what :mod:`db.locks` is for.

A lease expires on its own. An instance killed mid-fold therefore blocks the
next attempt for at most ``seconds``, rather than for ever.
"""

from __future__ import annotations

import logging
from typing import Any

from core.config import settings

logger = logging.getLogger(__name__)


# ── SCALING FIX #12 · one instance folds a summary, not both ──────────────────────────
# Why: docs/SCALING.md §12 · Test: docs/TESTING-SCALING.md §11
class LeaseStore:
    """Time-boxed named claims, held in Redis when there is a Redis."""

    def __init__(self, url: str = "", prefix: str = "cfa") -> None:
        self._url = url
        self._prefix = prefix
        self._redis: Any = None

    @property
    def enabled(self) -> bool:
        """Whether a lease actually excludes anyone."""
        return self._redis is not None

    async def connect(self) -> None:
        """Open the connection, or leave leases as a no-op."""
        if not self._url:
            logger.info(
                "REDIS_URL not set — no cross-instance leases; duplicate "
                "background work is deduplicated per process only"
            )
            return

        try:
            from redis.asyncio import Redis  # noqa: PLC0415 - optional dependency
        except ImportError:
            logger.warning("redis package missing — running without leases")
            return

        try:
            client = Redis.from_url(self._url, decode_responses=True)
            await client.ping()
        except Exception as error:
            logger.warning("Redis unreachable (%s) — running without leases", error)
            return

        self._redis = client
        logger.info("Cross-instance leases ready")

    async def close(self) -> None:
        """Hand the connection back at shutdown."""
        if self._redis is None:
            return
        client, self._redis = self._redis, None
        try:
            await client.aclose()
        except Exception:  # pragma: no cover - shutdown is best effort
            logger.debug("Closing the lease connection failed", exc_info=True)

    async def acquire(self, name: str, seconds: int) -> bool:
        """Claim ``name`` for ``seconds``. True means it is yours.

        True when leases are off, too — a caller must be correct either way.
        """
        if not self.enabled:
            return True
        try:
            # SET NX EX: the claim and its expiry in one round trip, so a
            # process that dies between them cannot leave a lease with no end.
            return bool(await self._redis.set(self._key(name), "1", nx=True, ex=seconds))
        except Exception as error:
            self._disable(error)
            return True

    async def release(self, name: str) -> None:
        """Give a lease back early, so the next attempt need not wait it out."""
        if not self.enabled:
            return
        try:
            await self._redis.delete(self._key(name))
        except Exception as error:
            self._disable(error)

    def _key(self, name: str) -> str:
        return f"{self._prefix}:lease:{name}"

    def _disable(self, error: Exception) -> None:
        """Fall back to per-process behaviour rather than failing the caller."""
        logger.warning("Leases disabled after a Redis error: %s", error)
        self._redis = None


leases = LeaseStore(url=settings.REDIS_URL, prefix=settings.REDIS_KEY_PREFIX)
