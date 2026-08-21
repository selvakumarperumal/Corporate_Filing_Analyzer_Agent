"""The nodes of the filing analysis graph.

Nodes stay thin: they call a service and write a progress event onto the
graph's custom stream so the UI can show each step as it happens.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from langgraph.config import get_stream_writer

from core.categories import DEFAULT_CATEGORY, label_of
from graph.state import FilingState

if TYPE_CHECKING:  # avoids a services <-> graph import cycle at runtime
    from services.analysis_service import AnalysisService
    from services.router_service import RouterService
    from services.vector_service import VectorService

logger = logging.getLogger(__name__)

# How much retrieved text to hand the LLM.
CONTEXT_CHAR_LIMIT = 3000
CHUNKS_PER_QUERY = 4

NO_FILING_MESSAGE = (
    "No filing is attached to this dossier yet.\n\n"
    "Attach a 10-K, 10-Q, 8-K or annual report and ask again — answers are "
    "drawn only from the filings in this dossier."
)


def _emit(event: str, **payload: Any) -> None:
    """Write an event onto the graph's custom stream, for the UI to pick up."""
    get_stream_writer()({"event": event, **payload})


class FilingNodes:
    """Builds the graph's node callables, bound to the app services."""

    def __init__(
        self,
        vector: VectorService,
        router: RouterService,
        analysis: AnalysisService,
    ) -> None:
        self.vector = vector
        self.router = router
        self.analysis = analysis

    async def retrieve(self, state: FilingState) -> FilingState:
        """Pull the most relevant filing chunks for the query.

        Runs before routing so the router classifies with the filing in hand and
        the analysis node inherits the same context.
        """
        _emit("status", stage="retrieve")

        documents = await self.vector.search(
            state["query"],
            session_id=state.get("session_id", ""),
            k=CHUNKS_PER_QUERY,
        )
        context = "\n\n".join(doc.page_content for doc in documents)[:CONTEXT_CHAR_LIMIT]

        logger.debug("Retrieved %d chars of context from %d chunk(s)", len(context), len(documents))
        return {"context": context}

    async def no_filing(self, state: FilingState) -> FilingState:
        """Terminal node for a chat that has nothing indexed.

        Without it the analysis prompt would run on an empty context and the
        model would invent a plausible-looking report out of nothing.
        """
        logger.info("No filing in this chat — asking for one instead of answering")
        _emit("token", content=NO_FILING_MESSAGE)
        return {"answer": NO_FILING_MESSAGE, "category": DEFAULT_CATEGORY}

    async def router_node(self, state: FilingState) -> FilingState:
        """Classify the query, and name the chat if it does not have a name yet.

        A chat is named once, after the question that opened it: every later run
        arrives carrying that name and leaves it alone. Naming runs alongside
        the classification rather than after it, so the first question in a chat
        does not wait any longer for its answer than the ones that follow.
        """
        _emit("status", stage="route")

        query = state["query"]
        title = (state.get("title") or "").strip()

        if title:
            category = await self.router.classify(query)
        else:
            category, title = await asyncio.gather(
                self.router.classify(query),
                self.router.name_chat(query),
            )
            _emit("title", title=title)

        _emit("route", category=category)
        return {"category": category, "title": title}

    def analysis_node(self, category: str) -> Callable:
        """Build the terminal node that runs ``category``-specific analysis.

        The answer's tokens reach the UI through LangGraph's ``messages`` stream
        mode, so this node just awaits the finished text for the final state.
        """

        async def node(state: FilingState) -> FilingState:
            _emit("status", stage="analyze", category=category)
            logger.info("Running %s analysis", label_of(category))

            answer = await self.analysis.analyze(
                category,
                state.get("context", ""),
                state.get("query", ""),
                summary=state.get("summary", ""),
                history=state.get("history", []),
            )
            return {"answer": answer, "category": category}

        node.__name__ = category
        return node
