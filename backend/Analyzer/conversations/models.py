"""Conversation tables: the dossiers an analyst opens, and the messages in them.

The message table is the source of truth for everything that has been said.
Rows, not one blob per conversation: a single JSON document is fine until
something needs to page through a conversation, edit one message, or count
tokens across a range — and then it is a rewrite. One row per message keeps all
of that a query.

Two ids name a conversation, and the distinction matters:

``id``
    ours, and what ``messages.conversation_id`` points at.
``client_id``
    the dossier id the browser generated. Unique *per account*, never on its
    own — two analysts may independently mint the same one — so nothing looks a
    conversation up by ``client_id`` without an owner beside it.

``Message.meta`` is a ``jsonb`` column. What hangs off a message varies by what
produced it (attached filings, the run that answered, the reason a run failed)
and none of it is queried structurally, so it lives there rather than as a
column each new kind of message would add.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import field_validator
from sqlalchemy import Column, Index, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from db.columns import timestamp_column as _timestamp
from db.columns import utcnow
from db.columns import uuid_hex as _uuid

# Roles as the LLM understands them. Kept as a plain string column rather than
# a database enum, so adding a role later is a deploy and not a migration.
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_SYSTEM = "system"
ROLES = frozenset({ROLE_USER, ROLE_ASSISTANT, ROLE_SYSTEM})

# A message that came back broken is still kept — the ledger should show the
# analyst that a run failed, rather than silently losing the question.
STATUS_OK = "complete"
STATUS_ERROR = "error"


def _json_column(name: str = "metadata") -> Column:
    """A ``jsonb`` column.

    ``jsonb`` rather than ``json``: Postgres stores it parsed, so it is read
    back without a parse per row and can be indexed into if anything ever needs
    to query one of these keys.
    """
    return Column(name, JSONB, nullable=False)


class Conversation(SQLModel, table=True):
    """One dossier: its name, its filings, and the head of its message log."""

    __tablename__ = "conversations"
    __table_args__ = (
        # The pair is what is unique, not the client id alone — see the module
        # docstring. It doubles as the index every lookup by dossier uses.
        UniqueConstraint("user_id", "client_id", name="uq_conversation_owner_client"),
        # The dock lists an analyst's dossiers most-recent first; this is the
        # index that ordering reads.
        Index("ix_conversation_owner_recent", "user_id", "last_message_at"),
    )

    id: str = Field(default_factory=_uuid, primary_key=True, max_length=32)
    user_id: str = Field(
        foreign_key="users.id", index=True, ondelete="CASCADE", max_length=32
    )
    client_id: str = Field(max_length=64)

    # Blank until the router names the dossier after its opening question.
    title: str = Field(default="", max_length=200)

    # The filings ingested into this dossier's vector collection, as
    # ``[{"name": ..., "chunks": ..., "added_at": ...}]``. Kept beside the
    # conversation so a returning analyst sees what the answers were drawn
    # from; the text itself stays in Chroma.
    filings: list = Field(default_factory=list, sa_column=_json_column("filings"))

    message_count: int = Field(default=0)

    # ── Rolling summary ──────────────────────────────────────────────────
    # Everything up to and including ``summary_through_seq``, compressed. A
    # long conversation is sent as this plus the last few turns, so the prompt
    # stays a fixed size however far back the dossier goes.
    summary: str = Field(default="", sa_column=Column("summary", Text, nullable=False))
    summary_through_seq: int = Field(default=0)
    summary_tokens: int = Field(default=0)

    created_at: datetime = Field(
        default_factory=utcnow, sa_column=_timestamp(nullable=False)
    )
    last_message_at: datetime = Field(
        default_factory=utcnow, sa_column=_timestamp(nullable=False)
    )


class Message(SQLModel, table=True):
    """One thing said in a conversation, by the analyst or by the analyzer."""

    __tablename__ = "messages"
    __table_args__ = (
        # Ordering and cursor pagination both run on this pair, and the
        # uniqueness is what stops two writers claiming the same position.
        UniqueConstraint("conversation_id", "seq", name="uq_message_position"),
    )

    id: str = Field(default_factory=_uuid, primary_key=True, max_length=32)
    conversation_id: str = Field(
        foreign_key="conversations.id", index=True, ondelete="CASCADE", max_length=32
    )
    # Position within the conversation, from 1. Ordering by ``created_at``
    # would be at the mercy of clock resolution — two messages in the same
    # millisecond are a coin toss — and pagination needs a total order.
    seq: int = Field()

    role: str = Field(max_length=16)
    content: str = Field(sa_column=Column("content", Text, nullable=False))
    # Estimated at write time; see :mod:`core.tokens` for why it is an estimate.
    tokens: int = Field(default=0)
    status: str = Field(default=STATUS_OK, max_length=16)
    meta: dict = Field(default_factory=dict, sa_column=_json_column())

    created_at: datetime = Field(
        default_factory=utcnow, sa_column=_timestamp(nullable=False)
    )

    @field_validator("role")
    @classmethod
    def _known_role(cls, value: str) -> str:
        if value not in ROLES:
            raise ValueError(f"Unknown role '{value}'. Expected one of {sorted(ROLES)}")
        return value
