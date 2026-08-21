"""Request and response bodies for the API.

These are the *validating* half of the SQLModel pair: unlike the table classes
in :mod:`models.user`, nothing here sets ``table=True``, so every field rule
actually runs. Inheriting ``UserBase`` is what keeps the email and name rules
identical between what a request may contain and what a row may hold.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import EmailStr
from sqlmodel import Field, SQLModel

from models.user import UserBase


class SignupRequest(UserBase):
    """What opening an account requires: the analyst's details, and a password.

    Length is the requirement that actually buys resistance to guessing;
    composition rules mostly buy predictable passwords.
    """

    password: str = Field(min_length=8, max_length=128)


class LoginRequest(SQLModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(SQLModel):
    refresh_token: str


class UserOut(UserBase):
    """The analyst as the API reports them — never the password hash."""

    id: str
    created_at: datetime


class TokenPair(SQLModel):
    """A fresh access/refresh pair, plus who it belongs to.

    ``expires_in`` is the access token's lifetime in seconds, so the client can
    refresh ahead of expiry instead of waiting for a 401.
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


# ── Conversations ────────────────────────────────────────────────────────


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
