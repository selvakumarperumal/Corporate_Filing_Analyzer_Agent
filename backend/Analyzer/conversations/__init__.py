"""Dossiers: the conversations an analyst opens and everything said in them.

The ledger (tables, service, cache) and the routes that read it. What is
*answered* into a dossier is the :mod:`analysis` package's business; this one
only records it and hands the right slice back.
"""

from conversations.cache import MessageCache, message_cache
from conversations.models import (
    ROLE_ASSISTANT,
    ROLE_SYSTEM,
    ROLE_USER,
    STATUS_ERROR,
    STATUS_OK,
    Conversation,
    Message,
)
from conversations.schemas import (
    ConversationOut,
    MessageOut,
    MessagePage,
    TitleUpdate,
)
from conversations.service import ContextHistory, HistoryService, history_service

__all__ = [
    "ROLE_ASSISTANT",
    "ROLE_SYSTEM",
    "ROLE_USER",
    "STATUS_ERROR",
    "STATUS_OK",
    "ContextHistory",
    "Conversation",
    "ConversationOut",
    "HistoryService",
    "Message",
    "MessageCache",
    "MessageOut",
    "MessagePage",
    "TitleUpdate",
    "history_service",
    "message_cache",
]
