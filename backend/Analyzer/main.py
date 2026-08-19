"""Corporate Filing Analyzer Agent — FastAPI + Socket.IO API Backend."""

from __future__ import annotations

import logging

import socketio
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from api.socket_handler import register_handlers
from core.config import settings
from core.logging_config import setup_logging
from graph.state import CATEGORIES, CATEGORY_DESCRIPTIONS, CATEGORY_LABELS
from services.chat_service import ChatService

# ── Logging ──────────────────────────────────────────────────────────
setup_logging()
logger = logging.getLogger(__name__)

# ── FastAPI ──────────────────────────────────────────────────────────
app = FastAPI(
    title="Corporate Filing Analyzer Agent API",
    description="API and Socket.IO backend for AI-powered corporate filing analysis.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Services ─────────────────────────────────────────────────────────
chat = ChatService.from_defaults(
    model=settings.OLLAMA_MODEL,
    embedding_model=settings.OLLAMA_EMBEDDING_MODEL,
    base_url=settings.OLLAMA_BASE_URL,
)
logger.info(
    "ChatService ready (model=%s, embeddings=%s)",
    settings.OLLAMA_MODEL,
    settings.OLLAMA_EMBEDDING_MODEL,
)

# ── HTTP Endpoints ───────────────────────────────────────────────────


@app.get("/")
async def root():
    """Root status endpoint."""
    return {
        "service": "Corporate Filing Analyzer Agent API",
        "status": "online",
        "version": "0.1.0",
        "docs": "/docs",
    }


@app.get("/api/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "ok",
        "model": settings.OLLAMA_MODEL,
        "embedding_model": settings.OLLAMA_EMBEDDING_MODEL,
    }


@app.get("/api/categories")
async def categories():
    """Analysis categories available as direct graph entry points."""
    return {
        "categories": [
            {
                "id": c,
                "label": CATEGORY_LABELS[c],
                "description": CATEGORY_DESCRIPTIONS[c],
            }
            for c in CATEGORIES
        ]
    }


@app.get("/api/graph")
async def graph_diagram():
    """Mermaid source for the compiled analysis graph (handy for docs/debug)."""
    return {"mermaid": chat.graph.get_graph().draw_mermaid()}


@app.post("/api/upload")
async def upload_file(
    file: UploadFile = File(...),
    session_id: str = Form(default=""),
):
    """Upload and ingest a corporate filing document (PDF, TXT, MD, CSV) into vector store.

    ``session_id`` ties the document to a chat session so retrieval can be
    scoped to the files attached to that conversation.
    """
    content = await file.read()
    result = await chat.upload_file(content, file.filename, session_id or None)
    logger.info("Uploaded %s → %d chunks", file.filename, result["chunks_ingested"])
    return result


# ── Socket.IO Server ─────────────────────────────────────────────────
sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
register_handlers(sio, chat)

# ── ASGI App (entry point) ───────────────────────────────────────────
asgi_app = socketio.ASGIApp(sio, other_asgi_app=app)
