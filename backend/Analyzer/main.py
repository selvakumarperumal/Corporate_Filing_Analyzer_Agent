"""Corporate Filing Analyzer Agent — FastAPI + Socket.IO backend.

Two ways in:
    HTTP      POST /api/auth/*, POST /api/upload, /api/conversations*,
              GET /api/health
    Socket.IO `query` event — ask a question, answer streams back

Everything past `/api/auth` and `/api/health` needs a signed-in analyst.
Filings are scoped twice over: to the account that uploaded them, and within
that account to the dossier they were attached to. Deleting the dossier
discards them.

Dossiers persist. Their messages are rows in the database, their filings are
collections in the vector store, and both survive a restart — so an analyst who
comes back tomorrow reopens the conversation where they left it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import socketio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.logging_config import setup_logging

# Configure logging before the app modules are imported, so the messages they
# log while loading (prompts, models, graph) are captured too.
setup_logging()

from api.auth_routes import router as auth_router  # noqa: E402
from api.chat_routes import router as chat_router  # noqa: E402
from api.deps import auth_service, chat_service  # noqa: E402
from api.socket_handler import register_handlers  # noqa: E402
from core.cache import message_cache  # noqa: E402
from core.config import settings  # noqa: E402
from core.database import SessionLocal, init_db  # noqa: E402
from services.chat_service import scoped_session_id  # noqa: E402

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the stores the app reads from, and clear what nothing owns."""
    await init_db()
    await message_cache.connect()
    await _prune_orphaned_filings()
    yield
    await message_cache.close()


async def _prune_orphaned_filings() -> None:
    """Drop vector collections no surviving dossier claims.

    Filings used to be cleared wholesale at startup, which was right when
    dossiers lasted only as long as the browser tab. Now that a dossier is a
    row, its filings have to outlive the process too — so what is cleared is
    only what nothing points at any more: collections whose dossier was deleted
    while the backend was down, or left behind by a crash between ingesting a
    file and recording it.
    """
    from sqlmodel import select  # noqa: PLC0415 - after logging is configured

    from models.conversation import Conversation

    try:
        async with SessionLocal() as session:
            result = await session.exec(
                select(Conversation.user_id, Conversation.client_id)
            )
            live = [
                scoped_session_id(user_id, client_id) for user_id, client_id in result.all()
            ]
        chat_service.vector.prune_to(live)
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

app.include_router(auth_router)
app.include_router(chat_router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    """Report that the API is up and which models it is using.

    Deliberately open: a health check that needs a login cannot be used by the
    thing that has to know whether logins are working.
    """
    return {
        "status": "ok",
        "model": settings.OLLAMA_MODEL,
        "embedding_model": settings.OLLAMA_EMBEDDING_MODEL,
    }


sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=settings.CORS_ORIGINS if settings.CORS_ORIGINS != ["*"] else "*",
)
register_handlers(sio, chat_service, auth_service)

# Entry point: `uvicorn main:asgi_app`
asgi_app = socketio.ASGIApp(sio, other_asgi_app=app)
logger.info("Backend ready — HTTP + Socket.IO mounted")
