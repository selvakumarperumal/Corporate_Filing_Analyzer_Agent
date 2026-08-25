"""The long-lived objects the app is built from, wired together once.

These are singletons because they own things that should exist once per
process: the chat model and its vector store, the compiled graph, the Redis
connection, the background summarisation tasks. Building them here rather than
in :mod:`main` is what lets a router, a Socket.IO handler or a script reach
them without importing the app module they happen to be mounted on.

This is the only module that decides *which* implementations the app runs with.
Everything else takes what it is given — the routes through
:mod:`api.dependencies`, so a test can override them.
"""

from __future__ import annotations

import logging

from analysis.pipeline import AnalysisPipeline
from auth.service import AuthService
from conversations.service import history_service
from core.config import settings

logger = logging.getLogger(__name__)

auth_service = AuthService()

analysis_pipeline = AnalysisPipeline(
    model=settings.OLLAMA_MODEL,
    embedding_model=settings.OLLAMA_EMBEDDING_MODEL,
    base_url=settings.OLLAMA_BASE_URL,
)

# The ledger reads and writes; the summariser it folds old turns with comes
# from the same model that answers, so it is attached once the pipeline exists
# rather than at import of the conversations package.
history_service.attach_summarizer(analysis_pipeline.summary.summarise)

__all__ = ["analysis_pipeline", "auth_service", "history_service"]
