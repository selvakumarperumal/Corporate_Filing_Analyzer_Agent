"""Dossier endpoints — listing, reading, renaming and discarding a dossier.

    GET    /api/conversations                   the analyst's dossiers
    GET    /api/conversations/{id}/messages     one page of a dossier's ledger
    PATCH  /api/conversations/{id}              rename a dossier
    DELETE /api/conversations/{id}              discard it, filings and all

Every route is scoped to the signed-in analyst: the dossier id in the path is
the browser's own, which is unique only per account, so it is never looked up
without an owner beside it.

Ingesting a filing is the one dossier-shaped operation that does *not* live
here — it belongs to the analysis pipeline that has to index the file, and is
in :mod:`analysis.routes`.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from analysis.pipeline import scoped_session_id
from api.dependencies import Analysis, CurrentUser, DbSession, History
from conversations.models import Conversation
from conversations.schemas import (
    ConversationOut,
    MessageOut,
    MessagePage,
    TitleUpdate,
)
from core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/conversations", tags=["dossiers"])


def _as_out(conversation: Conversation) -> ConversationOut:
    """A conversation row as the dock reads it."""
    return ConversationOut(
        id=conversation.client_id,
        title=conversation.title,
        message_count=conversation.message_count,
        filings=list(conversation.filings or []),
        created_at=conversation.created_at,
        last_message_at=conversation.last_message_at,
    )


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    user: CurrentUser, session: DbSession, history: History
) -> list[ConversationOut]:
    """The analyst's dossiers, most recently spoken in first.

    Enough to redraw the dock on sign-in; the messages themselves are fetched
    per dossier, as each is opened.
    """
    conversations = await history.list_conversations(session, user.id)
    return [_as_out(conversation) for conversation in conversations]


@router.get("/{session_id}/messages", response_model=MessagePage)
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
    :class:`conversations.service.HistoryService`.

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
        messages=[
            MessageOut.model_validate(message, from_attributes=True)
            for message in messages
        ],
        next_before_seq=next_before_seq,
    )


@router.patch("/{session_id}", response_model=ConversationOut)
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
    return _as_out(conversation)


@router.delete("/{session_id}")
async def delete_conversation(
    session_id: str,
    user: CurrentUser,
    session: DbSession,
    analysis: Analysis,
    history: History,
) -> dict[str, object]:
    """Discard a dossier: its messages, and the filings it could read.

    Both halves go, and in that order — a conversation whose messages were kept
    while its filings were dropped would answer follow-ups out of a summary of
    documents it can no longer cite.
    """
    deleted = await history.delete_conversation(session, user.id, session_id)
    dropped = analysis.delete_session(scoped_session_id(user.id, session_id))

    return {
        "status": "ok",
        "session_id": session_id,
        "deleted": deleted,
        "filings_dropped": bool(dropped.get("deleted")),
    }
