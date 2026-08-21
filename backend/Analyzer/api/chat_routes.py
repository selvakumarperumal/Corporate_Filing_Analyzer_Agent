"""Dossier endpoints — the filings in a conversation, and its message history.

    POST   /api/upload                          ingest a filing into a dossier
    GET    /api/conversations                   the analyst's dossiers
    GET    /api/conversations/{id}/messages     one page of a dossier's ledger
    PATCH  /api/conversations/{id}              rename a dossier
    DELETE /api/conversations/{id}              discard it, filings and all

Every route is scoped to the signed-in analyst: the dossier id in the path is
the browser's own, which is unique only per account, so it is never looked up
without an owner beside it.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from api.deps import Chat, CurrentUser, DbSession, History
from core.config import settings
from models.schemas import ConversationOut, MessageOut, MessagePage, TitleUpdate
from services.chat_service import scoped_session_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["dossiers"])


@router.post("/upload")
async def upload_file(
    user: CurrentUser,
    session: DbSession,
    chat: Chat,
    history: History,
    file: Annotated[UploadFile, File()],
    session_id: Annotated[str, Form()],
) -> dict[str, object]:
    """Ingest a filing (PDF, TXT, MD or CSV) into one dossier's collection.

    ``session_id`` is required: a filing always belongs to the dossier it was
    attached to, and is only ever retrieved for that dossier, under the account
    that uploaded it. Attaching a filing is also enough to open a dossier — an
    analyst may upload before they ask anything.
    """
    session_id = session_id.strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename given")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail=f"{file.filename} is empty")

    try:
        result = await chat.upload_file(
            content, file.filename, scoped_session_id(user.id, session_id)
        )
    except ValueError as error:
        # Unsupported file type, or a file we could not read any text out of.
        logger.warning("Upload rejected (%s): %s", file.filename, error)
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        # Anything else — the embedding model being unreachable, say. Answered
        # as JSON carrying the reason, because the browser only has what this
        # response says to show the analyst.
        logger.exception("Upload failed (%s)", file.filename)
        raise HTTPException(
            status_code=500,
            detail=f"Could not add {file.filename}: {error}",
        ) from error

    # Recorded after the ingest, so the register never lists a filing that is
    # not actually searchable.
    conversation = await history.open_conversation(session, user.id, session_id)
    await history.record_filing(
        session, conversation, file.filename, int(result["chunks_ingested"])
    )

    logger.info(
        "Uploaded %s -> %d chunks (user=%s)",
        file.filename,
        result["chunks_ingested"],
        user.id,
    )
    # The scoped id is a backend detail; the browser gets back the id it sent.
    return {**result, "session_id": session_id}


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    user: CurrentUser, session: DbSession, history: History
) -> list[ConversationOut]:
    """The analyst's dossiers, most recently spoken in first.

    Enough to redraw the dock on sign-in; the messages themselves are fetched
    per dossier, as each is opened.
    """
    conversations = await history.list_conversations(session, user.id)
    return [
        ConversationOut(
            id=conversation.client_id,
            title=conversation.title,
            message_count=conversation.message_count,
            filings=list(conversation.filings or []),
            created_at=conversation.created_at,
            last_message_at=conversation.last_message_at,
        )
        for conversation in conversations
    ]


@router.get("/conversations/{session_id}/messages", response_model=MessagePage)
async def list_messages(
    session_id: str,
    user: CurrentUser,
    session: DbSession,
    history: History,
    limit: Annotated[
        int, Query(ge=1, le=settings.HISTORY_MAX_PAGE_SIZE)
    ] = settings.HISTORY_PAGE_SIZE,
    before_seq: Annotated[int | None, Query(ge=1)] = None,
) -> MessagePage:
    """One page of a dossier's ledger, oldest first, newest page by default.

    This is the *display* history — everything that was said, untrimmed. What
    a run is actually sent is a much smaller thing assembled by
    :class:`services.history_service.HistoryService`.

    Page backwards by passing the previous response's ``next_before_seq``.
    """
    conversation = await history.find(session, user.id, session_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="No such dossier")

    messages = await history.page_messages(
        session, conversation, limit=limit, before_seq=before_seq
    )

    # There is an earlier page only if this one did not reach the first
    # message. Reading the oldest seq off the page beats a second COUNT query.
    oldest = messages[0].seq if messages else None
    next_before_seq = oldest if oldest is not None and oldest > 1 else None

    return MessagePage(
        session_id=session_id,
        messages=[MessageOut.model_validate(message, from_attributes=True) for message in messages],
        next_before_seq=next_before_seq,
    )


@router.patch("/conversations/{session_id}", response_model=ConversationOut)
async def rename_conversation(
    session_id: str,
    body: TitleUpdate,
    user: CurrentUser,
    session: DbSession,
    history: History,
) -> ConversationOut:
    """Rename a dossier, overriding the name the analyzer gave it."""
    conversation = await history.find(session, user.id, session_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="No such dossier")

    conversation = await history.set_title(session, conversation, body.title)
    return ConversationOut(
        id=conversation.client_id,
        title=conversation.title,
        message_count=conversation.message_count,
        filings=list(conversation.filings or []),
        created_at=conversation.created_at,
        last_message_at=conversation.last_message_at,
    )


@router.delete("/conversations/{session_id}")
async def delete_conversation(
    session_id: str, user: CurrentUser, session: DbSession, chat: Chat, history: History
) -> dict[str, object]:
    """Discard a dossier: its messages, and the filings it could read.

    Both halves go, and in that order — a conversation whose messages were kept
    while its filings were dropped would answer follow-ups out of a summary of
    documents it can no longer cite.
    """
    deleted = await history.delete_conversation(session, user.id, session_id)
    dropped = chat.delete_session(scoped_session_id(user.id, session_id))

    return {
        "status": "ok",
        "session_id": session_id,
        "deleted": deleted,
        "filings_dropped": bool(dropped.get("deleted")),
    }

