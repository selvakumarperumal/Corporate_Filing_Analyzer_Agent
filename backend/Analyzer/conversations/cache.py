"""Optional Redis cache of each conversation's hot tail.

Every run needs the last few turns of the conversation it belongs to, and it
needs them before it can start answering. That read is the same handful of rows
over and over for an active dossier, so it is the one worth keeping in memory.

The database stays the source of truth. This cache holds a window of the most
recent messages per conversation and nothing else: lose it, flush it, or never
configure it, and the only difference is that reads go to Postgres instead. So
every operation here fails soft — a Redis that is down must not take the chat
down with it — and a cache error disables the cache rather than raising into
the request.

Off unless ``REDIS_URL`` is set.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from core.config import settings

logger = logging.getLogger(__name__)


class MessageCache:
    """The recent messages of each conversation, if Redis is configured.

    The key holds the tail as a list, newest last, trimmed to
    ``REDIS_HOT_WINDOW``. Because the whole key expires at once, a key that
    exists holds the full window (or the whole conversation, if it is shorter)
    — so a hit never has to wonder whether it is looking at a partial tail.
    """

    def __init__(
        self,
        url: str = "",
        prefix: str = "cfa",
        window: int = 40,
        ttl_seconds: int = 3600,
    ) -> None:
        self._url = url
        self._prefix = prefix
        self._window = window
        self._ttl = ttl_seconds
        self._redis: Any = None

    @property
    def enabled(self) -> bool:
        """Whether reads and writes will actually reach Redis."""
        return self._redis is not None

    async def connect(self) -> None:
        """Open the connection, or leave the cache off.

        Called once at startup. A missing package, a bad URL or an unreachable
        server are all logged and shrugged off: the app runs without a cache.
        """
        if not self._url:
            logger.info("REDIS_URL not set — running without a message cache")
            return

        try:
            from redis.asyncio import Redis
        except ImportError:
            logger.warning(
                "REDIS_URL is set but the redis package is not installed "
                "(`uv add redis`) — running without a message cache"
            )
            return

        try:
            client = Redis.from_url(self._url, decode_responses=True)
            await client.ping()
        except Exception as error:
            logger.warning("Redis unreachable (%s) — running without a cache", error)
            return

        self._redis = client
        logger.info(
            "Message cache ready (window=%d messages, ttl=%ds)", self._window, self._ttl
        )

    async def close(self) -> None:
        """Hand the connections back at shutdown."""
        if self._redis is None:
            return
        client, self._redis = self._redis, None
        try:
            await client.aclose()
        except Exception:  # pragma: no cover - shutdown is best effort
            logger.debug("Closing Redis failed", exc_info=True)

    async def recent(self, conversation_id: str) -> list[dict] | None:
        """The cached tail for a conversation, or ``None`` to read the database.

        A conversation with no messages is never cached — Redis has no such
        thing as an empty list — so the first run in a dossier always misses,
        which costs one query against a table it is about to write to anyway.
        """
        if not self.enabled:
            return None

        key = self._key(conversation_id)
        try:
            raw = await self._redis.lrange(key, 0, -1)
            if not raw:
                return None
            # Touch the window on every read, so an active dossier stays hot
            # and an abandoned one falls out on its own.
            await self._redis.expire(key, self._ttl)
        except Exception as error:
            self._disable(error)
            return None

        try:
            return [json.loads(item) for item in raw]
        except json.JSONDecodeError:
            # Something else wrote this key, or the format changed under us.
            logger.warning("Discarding unreadable cache entry for %s", conversation_id)
            await self.drop(conversation_id)
            return None

    async def prime(self, conversation_id: str, messages: list[dict]) -> None:
        """Replace the cached tail with the one just read from the database."""
        if not self.enabled:
            return

        key = self._key(conversation_id)
        tail = messages[-self._window :]
        if not tail:
            return
        try:
            # One pipeline, so a reader never sees the key half-rewritten.
            pipe = self._redis.pipeline()
            pipe.delete(key)
            pipe.rpush(key, *[json.dumps(m, default=str) for m in tail])
            pipe.expire(key, self._ttl)
            await pipe.execute()
        except Exception as error:
            self._disable(error)

    async def append(self, conversation_id: str, message: dict) -> None:
        """Add one message to the tail, if this conversation is cached.

        Deliberately does *not* create the key: a conversation nobody has read
        yet would end up cached as a one-message tail, which a later read would
        take for the whole window and answer from. Priming is what creates it.
        """
        if not self.enabled:
            return

        key = self._key(conversation_id)
        try:
            if not await self._redis.exists(key):
                return
            pipe = self._redis.pipeline()
            pipe.rpush(key, json.dumps(message, default=str))
            pipe.ltrim(key, -self._window, -1)
            pipe.expire(key, self._ttl)
            await pipe.execute()
        except Exception as error:
            self._disable(error)

    async def drop(self, conversation_id: str) -> None:
        """Forget a conversation — deleted, or changed behind the cache's back."""
        if not self.enabled:
            return
        try:
            await self._redis.delete(self._key(conversation_id))
        except Exception as error:
            self._disable(error)

    def _key(self, conversation_id: str) -> str:
        return f"{self._prefix}:conv:{conversation_id}:tail"

    def _disable(self, error: Exception) -> None:
        """Drop the cache after a failure rather than failing the request.

        Reconnecting on every subsequent message would mean paying a timeout
        per request for as long as Redis is down. Serving from the database is
        both correct and faster than that; the cache comes back on restart.
        """
        logger.warning("Message cache disabled after a Redis error: %s", error)
        self._redis = None


message_cache = MessageCache(
    url=settings.REDIS_URL,
    prefix=settings.REDIS_KEY_PREFIX,
    window=settings.REDIS_HOT_WINDOW,
    ttl_seconds=settings.REDIS_TTL_SECONDS,
)
