"""Corporate Filing Analyzer Agent — FastAPI + Socket.IO backend.

Two ways in:
    HTTP      POST /api/auth/*, POST /api/upload, /api/conversations*,
              GET /api/health
    Socket.IO `query` event — ask a question, answer streams back

Everything past `/api/auth` and `/api/health` needs a signed-in analyst.
Filings are scoped twice over: to the account that uploaded them, and within
that account to the dossier they were attached to. Deleting the dossier
discards them.

Dossiers persist. Their messages are rows in Postgres, their filings are
collections in the vector store, and both survive a restart — so an analyst who
comes back tomorrow reopens the conversation where they left it.

This module is only the assembly: the app, its middleware, the lifespan, and
the two protocols mounted side by side. What it assembles is built in
:mod:`container` and lives in the domain packages — :mod:`auth`,
:mod:`conversations`, :mod:`analysis`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import socketio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.logging import setup_logging

# Configure logging before the app modules are imported, so the messages they
# log while loading (prompts, models, graph) are captured too.
setup_logging()

from api.dependencies import auth_service  # noqa: E402
from api.routes import api_router  # noqa: E402
from api.socket import drain, register_handlers  # noqa: E402
from container import analysis_pipeline, history_service  # noqa: E402
from conversations.cache import message_cache  # noqa: E402
from core.config import settings  # noqa: E402
from core.leases import leases  # noqa: E402
from db.engine import SessionLocal, init_db  # noqa: E402
from db.locks import only_one  # noqa: E402

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the stores the app reads from, tidy up, and leave cleanly.

    Written for more than one instance of this process running at once, which
    is mostly a matter of what happens *once* rather than once per instance.
    Schema creation is serialised (:func:`db.engine.init_db`); the two
    housekeeping jobs take an advisory lock and skip if another instance holds
    it. Neither is allowed to fail a startup.
    """
    _check_signing_key()
    await init_db()
    await message_cache.connect()
    await leases.connect()

    # Under one lock, because they are the same job seen from two sides:
    # forgetting filings nothing points at, and answering questions nothing
    # came back for. Whichever instance wins does both; the rest get on with
    # serving. Neither is urgent enough to wait for.
    async with only_one("startup-housekeeping") as mine:
        if mine:
            await _prune_orphaned_filings()
            await _close_out_interrupted_runs()
        else:
            logger.info("Another instance is doing the startup housekeeping")

    yield

    # New connections have already stopped arriving by here; what is left is
    # answers still being written. Waiting for them is the difference between a
    # rollout that costs a reconnect and one that costs an answer.
    await drain(settings.SHUTDOWN_DRAIN_SECONDS)
    await leases.close()
    await message_cache.close()


def _check_signing_key() -> None:
    """Say at startup, not at the first login, if tokens are unsigned by config.

    :func:`auth.security._secret` warns the first time it is asked for a key,
    which on a quiet instance can be hours after it started — long enough for a
    deploy check to read the logs and conclude everything is fine. Asking here
    means the warning is in the first few lines or not at all.

    Not fatal. A single instance with an ephemeral key works, at the price of
    signing everyone out on restart; several instances with one each reject
    each other's tokens, which is docs/SCALING.md Break #1 and the reason this
    line exists at all.
    """
    if settings.JWT_SECRET_KEY:
        return
    # Provokes the warning in auth.security, where the explanation lives.
    from auth.security import _secret  # noqa: PLC0415 - startup-only check

    _secret()
    logger.warning(
        "Tokens are signed with a key that dies with this process. Running "
        "more than one instance this way means logins fail at random — set "
        "JWT_SECRET_KEY to the same value everywhere."
    )


async def _close_out_interrupted_runs() -> None:
    """Answer the questions whose runs did not survive a previous process."""
    try:
        await history_service.sweep_interrupted_runs()
    except Exception:
        logger.exception("Could not sweep interrupted runs — leaving them as they are")


async def _prune_orphaned_filings() -> None:
    """Drop vector collections no surviving dossier claims.

    Filings used to be cleared wholesale at startup, which was right when
    dossiers lasted only as long as the browser tab. Now that a dossier is a
    row, its filings have to outlive the process too — so what is cleared is
    only what nothing points at any more: collections whose dossier was deleted
    while the backend was down, or left behind by a crash between ingesting a
    file and recording it.

    Two things make this safe with a shared store and several instances. The
    caller holds an advisory lock, so one instance prunes and the others skip;
    and an upload opens its dossier row *before* it creates the collection
    (see :mod:`analysis.routes`), so a filing being ingested right now is never
    mistaken for an orphan.
    """
    from sqlmodel import select  # noqa: PLC0415 - after logging is configured

    from analysis.pipeline import scoped_session_id  # noqa: PLC0415
    from conversations.models import Conversation  # noqa: PLC0415

    try:
        async with SessionLocal() as session:
            result = await session.exec(
                select(Conversation.user_id, Conversation.client_id)
            )
            live = [
                scoped_session_id(user_id, client_id)
                for user_id, client_id in result.all()
            ]
        # In a thread: Chroma's client is synchronous, and against a shared
        # server this walks every collection over HTTP.
        await asyncio.to_thread(analysis_pipeline.vector.prune_to, live)
    except Exception:
        # Startup housekeeping. Failing it would cost the analyst their app
        # over disk that is merely untidy.
        logger.exception("Could not prune orphaned filings — leaving them in place")


app = FastAPI(
    title="Corporate Filing Analyzer Agent API",
    description="AI-powered analysis of corporate filings.",
    version="0.3.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


def _client_manager() -> socketio.AsyncManager | None:
    """A Redis client manager, if one has been asked for.

    What it buys: room membership replicated through Redis pub/sub, so an
    ``emit`` to a room reaches connections held by other instances.

    Why it is off by default: nothing here broadcasts. Every event goes to the
    one connection that asked for it — ``emit(..., to=sid)`` — from the very
    process holding that connection, so there is nothing to route between
    instances and a Redis hop per token would be pure cost.

    Turn it on with the first ``enter_room``: a shared dossier watched live, an
    analyst on two devices, a background job pushing a notification. Note what
    it still does *not* do — ``save_session``/``get_session`` stay in this
    process's memory even with a manager attached. Nothing here needs them
    shared (a connection is read only by the instance holding it), and the
    access token is a JWT any instance can decode if that ever changes.
    """
    url = settings.SOCKETIO_MESSAGE_QUEUE_URL.strip()
    if not url:
        return None
    logger.info("Socket.IO client manager on %s", url.split("@")[-1])
    return socketio.AsyncRedisManager(url)


sio = socketio.AsyncServer(
    async_mode="asgi",
    client_manager=_client_manager(),
    cors_allowed_origins=settings.CORS_ORIGINS if settings.CORS_ORIGINS != ["*"] else "*",
)
register_handlers(sio, analysis_pipeline, auth_service)

# Entry point: `uvicorn main:asgi_app`
asgi_app = socketio.ASGIApp(sio, other_asgi_app=app)
logger.info("Backend ready — HTTP + Socket.IO mounted")
