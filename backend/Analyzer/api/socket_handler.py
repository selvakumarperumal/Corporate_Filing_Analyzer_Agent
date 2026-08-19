"""Socket.IO event handlers for real-time chat."""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

import socketio

from graph.state import CATEGORIES, CATEGORY_LABELS
from services.chat_service import ChatService

logger = logging.getLogger(__name__)

# Used when a category is selected (or a file attached) without any typed text.
_CATEGORY_FALLBACK_QUERY = {
    "financials": "Extract the key financial metrics and performance highlights.",
    "compliance": "Report the auditor's opinion, internal controls, and compliance matters.",
    "risks": "List and prioritise the disclosed risk factors.",
    "shareholding": "Summarise the shareholding pattern and ownership structure.",
    "governance": "Summarise board composition, committees, and executive compensation.",
    "mda": "Summarise the Management Discussion & Analysis section.",
    "summary": "Provide an executive summary of this filing.",
    "qa": "Provide an executive summary and financial overview of this filing.",
}


def register_handlers(sio: socketio.AsyncServer, chat: ChatService) -> None:
    """Register all Socket.IO event handlers."""

    async def _pump(sid: str, events: AsyncIterator[dict[str, Any]]) -> None:
        """Forward graph events to the client as Socket.IO emissions."""
        async for event in events:
            payload = dict(event)
            event_type = payload.pop("event")
            await sio.emit(event_type, payload, to=sid)

    @sio.event
    async def connect(sid: str, environ: dict) -> None:
        logger.info("Client connected: %s", sid)
        await sio.emit(
            "connected",
            {
                "message": "Connected to Corporate Filing Analyzer",
                "categories": [
                    {"id": c, "label": CATEGORY_LABELS[c]} for c in CATEGORIES
                ],
            },
            to=sid,
        )

    @sio.event
    async def disconnect(sid: str) -> None:
        logger.info("Client disconnected: %s", sid)

    @sio.event
    async def query(sid: str, data: dict[str, Any]) -> None:
        """Handle an incoming query — run the graph and stream its events.

        Payload:
            query:            the analyst's question (may be empty if a
                              category was picked or a file was attached)
            category:         "auto" for LLM routing, or a category id to jump
                              straight to that node
            require_approval: pause on the auto-routing decision for approval
            session_id:       chat session, scopes retrieval to its uploads
        """
        user_query = (data.get("query") or "").strip()
        raw_category = (data.get("category") or "auto").strip().lower()
        category = raw_category if raw_category in CATEGORIES else None

        if not user_query:
            user_query = _CATEGORY_FALLBACK_QUERY.get(
                category or "qa", _CATEGORY_FALLBACK_QUERY["qa"]
            )

        logger.info(
            "Query from %s (category=%s, approval=%s): %s",
            sid,
            category or "auto",
            data.get("require_approval"),
            user_query[:80],
        )

        try:
            await _pump(
                sid,
                chat.query_stream(
                    user_query,
                    session_id=data.get("session_id"),
                    category=category,
                    require_approval=bool(data.get("require_approval")),
                ),
            )
        except Exception as e:
            logger.exception("Error processing query from %s", sid)
            await sio.emit("error", {"message": str(e)}, to=sid)

    @sio.event
    async def resume(sid: str, data: dict[str, Any]) -> None:
        """Resume a paused run once the human approves or overrides the route."""
        run_id = (data.get("run_id") or "").strip()
        if not run_id:
            await sio.emit("error", {"message": "Missing run_id for resume"}, to=sid)
            return

        category = (data.get("category") or "").strip().lower() or None
        logger.info("Resume run %s from %s (category=%s)", run_id, sid, category)

        try:
            await _pump(sid, chat.resume_stream(run_id, category=category))
        except Exception as e:
            logger.exception("Error resuming run %s", run_id)
            await sio.emit("error", {"message": str(e)}, to=sid)
