"""Request and response bodies for the dossier endpoints.

The validating half of the pair: the tables are in :mod:`conversations.models`,
and none of these set ``table=True``, so every field rule here actually runs.
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Field, SQLModel


class ConversationOut(SQLModel):
    """A dossier as the dock lists it.

    ``id`` is the client id the browser minted, not the row's primary key: the
    browser addresses its dossiers by the id it already has, and our internal
    id is nothing it needs to know.
    """

    id: str
    title: str
    message_count: int
    filings: list[dict] = Field(default_factory=list)
    created_at: datetime
    last_message_at: datetime


class MessageOut(SQLModel):
    """One message from the ledger, as the client redraws it."""

    id: str
    seq: int
    role: str
    content: str
    tokens: int
    status: str
    meta: dict = Field(default_factory=dict)
    created_at: datetime


class MessagePage(SQLModel):
    """A page of display history, oldest first.

    ``next_before_seq`` is the cursor for the page *before* this one — history
    is read backwards from the end — and is null once the start is reached.
    """

    session_id: str
    messages: list[MessageOut]
    next_before_seq: int | None = None


class TitleUpdate(SQLModel):
    """Rename a dossier by hand, overriding the name the analyzer gave it."""

    title: str = Field(min_length=1, max_length=200)
