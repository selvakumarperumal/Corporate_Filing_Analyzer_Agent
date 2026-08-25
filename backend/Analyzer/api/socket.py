"""Socket.IO event handlers for real-time chat.

The connection itself is what is authenticated: a client hands over an access
token in the handshake, and a handshake without a valid one is refused before
any event can be sent. The user it resolved to is then held against the socket,
so every query on that connection is answered for the account that opened it
and reads only that account's filings.

A run is also where the ledger is written. The question is recorded before the
graph starts and the answer once it finishes, each in its own short-lived
database session — an answer can take a minute, and a pooled connection held
open across it is a connection nothing else can use.
"""

from __future__ import annotations

import logging
from typing import Any

import socketio

from analysis.pipeline import AnalysisPipeline, scoped_session_id
from auth.security import AuthError
from auth.service import AuthService
from conversations.models import (
    ROLE_ASSISTANT,
    ROLE_USER,
    STATUS_ERROR,
    STATUS_OK,
    Conversation,
)
from conversations.service import history_service
from db.engine import SessionLocal

logger = logging.getLogger(__name__)

# Used when a filing is attached with no typed question — the router still
# needs something to classify.
FALLBACK_QUERY = "Provide an executive summary and financial overview of this filing."


def register_handlers(
    sio: socketio.AsyncServer,
    analysis: AnalysisPipeline,
    auth: AuthService,
) -> None:
    """Register the handlers for the `connect`, `disconnect` and `query` events."""

    @sio.event
    async def connect(sid: str, environ: dict, auth_data: dict | None = None) -> None:
        """Admit the connection only if it carries a usable access token.

        Raising ConnectionRefusedError is what makes the handshake fail: the
        browser sees `connect_error` and knows to refresh its token and retry,
        rather than sitting on a connection that would reject every query.
        """
        token = _token_from(auth_data, environ)
        if not token:
            logger.info("Handshake from %s refused — no token", sid)
            raise socketio.exceptions.ConnectionRefusedError("Not signed in.")

        try:
            async with SessionLocal() as session:
                user = await auth.user_from_access_token(session, token)
        except AuthError as error:
            logger.info("Handshake from %s refused — %s", sid, error)
            raise socketio.exceptions.ConnectionRefusedError(str(error)) from error

        await sio.save_session(sid, {"user_id": user.id, "email": user.email})
        logger.info("Client connected: %s (user=%s)", sid, user.email)

    @sio.event
    async def disconnect(sid: str) -> None:
        logger.info("Client disconnected: %s", sid)

    @sio.event
    async def query(sid: str, data: dict[str, Any]) -> None:
        """Run a question through the graph, stream it back, and record it.

        Payload::

            query:      the analyst's question (may be empty if a file was
                        attached without one)
            session_id: the dossier, which scopes retrieval to its uploads and
                        names the conversation the run is written into
            title:      the name this dossier already carries, blank if it has
                        none yet — a dossier with no name is named on this run
            files:      names of the filings attached to this question, if any,
                        recorded alongside it so the ledger redraws intact

        Every event emitted back carries the ``session_id`` it was produced
        for, so a client that has moved on to another dossier can tell a late
        answer from one meant for the dossier now on screen and drop it. The id
        that goes back is the client's own — the account it is scoped under on
        the backend is not the browser's business.
        """
        question = (data.get("query") or "").strip() or FALLBACK_QUERY
        session_id = (data.get("session_id") or "").strip()
        title = (data.get("title") or "").strip()
        attachments = [str(name) for name in (data.get("files") or [])][:20]

        socket_session = await sio.get_session(sid)
        user_id = socket_session.get("user_id") if socket_session else None
        if not user_id:
            # Only reachable if the session went missing after a valid
            # handshake; the client should reconnect and sign in again.
            await sio.emit(
                "error",
                {"message": "Your session expired — sign in again.", "session_id": session_id},
                to=sid,
            )
            return

        # A query with no dossier behind it has no filings it is entitled to
        # read, so it is refused rather than answered from nothing.
        if not session_id:
            logger.warning("Query from %s carried no session id — refused", sid)
            await sio.emit(
                "error",
                {"message": "This chat has no id — reload the page and try again."},
                to=sid,
            )
            return

        logger.info("Query from %s (chat=%s): %r", sid, session_id, question[:80])

        try:
            async with SessionLocal() as db:
                conversation = await history_service.open_conversation(
                    db, user_id, session_id, title
                )
                conversation_pk = conversation.id
                # The stored name wins over the one the browser sent: the
                # server named this dossier, and a client that has fallen
                # behind should not be able to have it renamed.
                title = conversation.title or title

                # Assembled *before* the question is recorded — it is being
                # asked now, and would otherwise arrive in the prompt twice.
                history = await history_service.context_for(db, conversation)
                await history_service.record_message(
                    db,
                    conversation,
                    ROLE_USER,
                    question,
                    meta={"files": attachments} if attachments else {},
                )
        except Exception:
            logger.exception("Could not open the ledger for chat %s", session_id)
            await sio.emit(
                "error",
                {
                    "message": "Could not open this dossier's history — try again.",
                    "session_id": session_id,
                },
                to=sid,
            )
            return

        answer: list[str] = []
        category = ""
        run_id = ""
        failure = ""

        try:
            async for event in analysis.query_stream(
                question,
                session_id=scoped_session_id(user_id, session_id),
                title=title,
                history=history,
            ):
                payload = dict(event)
                event_name = payload.pop("event")
                payload["session_id"] = session_id

                # Kept as the stream goes by, so what is written to the ledger
                # is exactly what the analyst was shown.
                if event_name == "token":
                    answer.append(payload.get("content", ""))
                elif event_name == "run_started":
                    run_id = payload.get("run_id", "")
                elif event_name in {"route", "done"}:
                    category = payload.get("category") or category
                    title = payload.get("title") or title

                await sio.emit(event_name, payload, to=sid)
        except Exception as error:
            logger.exception("Query from %s failed", sid)
            failure = str(error)
            await sio.emit("error", {"message": failure, "session_id": session_id}, to=sid)

        await _record_answer(
            conversation_pk,
            "".join(answer),
            category=category,
            run_id=run_id,
            title=title,
            failure=failure,
        )


async def _record_answer(
    conversation_pk: str,
    answer: str,
    category: str,
    run_id: str,
    title: str,
    failure: str,
) -> None:
    """Write the answer — or the reason there wasn't one — into the ledger.

    A run that failed is still recorded, marked as such: the analyst should see
    on their next visit that the question was asked and did not land, rather
    than find it missing. The failed turn is kept out of what later runs are
    sent (see :meth:`HistoryService.context_for`) — it is a record, not
    something to answer from.

    Never raises. Losing the record of an answer the analyst has already read
    is not worth turning into an error they have to act on.
    """
    try:
        async with SessionLocal() as db:
            conversation = await db.get(Conversation, conversation_pk)
            if conversation is None:  # deleted mid-run
                return

            if title and title != conversation.title:
                conversation = await history_service.set_title(db, conversation, title)

            content = answer.strip() or failure or "No answer came back for this question."
            await history_service.record_message(
                db,
                conversation,
                ROLE_ASSISTANT,
                content,
                status=STATUS_ERROR if failure else STATUS_OK,
                meta={
                    key: value
                    for key, value in (
                        ("category", category),
                        ("run_id", run_id),
                        ("error", failure),
                    )
                    if value
                },
            )

        # Only once the answer is safely stored, and only after it has been
        # delivered: folding old turns is another model call, and no analyst
        # should be kept waiting on it.
        history_service.schedule_summary(conversation_pk)
    except Exception:
        logger.exception("Could not record the answer for conversation %s", conversation_pk)


def _token_from(auth_data: dict | None, environ: dict) -> str:
    """Pull the access token out of the handshake.

    Socket.IO's own `auth` payload is where a browser client puts it; the
    Authorization header is accepted too, for non-browser clients that have no
    handshake payload to fill in.
    """
    if auth_data:
        token = str(auth_data.get("token") or "").strip()
        if token:
            return token.removeprefix("Bearer ").strip()

    header = environ.get("HTTP_AUTHORIZATION", "")
    if header.startswith("Bearer "):
        return header[7:].strip()
    return ""
