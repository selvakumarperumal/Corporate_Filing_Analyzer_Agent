"""Node implementations for the filing analysis graph.

Nodes are thin: they delegate to the existing services and write progress
events onto the graph's custom stream so the UI can render each hop of the
pipeline as it happens.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from langgraph.config import get_stream_writer
from langgraph.types import interrupt

from graph.state import (
    CATEGORIES,
    CATEGORY_LABELS,
    DEFAULT_CATEGORY,
    FilingState,
)

if TYPE_CHECKING:  # avoids a services ⇄ graph import cycle at runtime
    from services.analysis_service import AnalysisService
    from services.router_service import RouterService
    from services.vector_service import VectorService

logger = logging.getLogger(__name__)


def _emit(event: str, **payload: Any) -> None:
    """Write an event onto the graph's custom stream (no-op outside a run)."""
    try:
        writer = get_stream_writer()
    except Exception:  # pragma: no cover - only when called outside a graph run
        return
    if writer is not None:
        writer({"event": event, **payload})


class FilingNodes:
    """Factory for the graph's node callables, bound to the app services."""

    def __init__(
        self,
        vector: "VectorService",
        router: "RouterService",
        analysis: "AnalysisService",
    ) -> None:
        self.vector = vector
        self.router = router
        self.analysis = analysis

    # ── Retrieval ────────────────────────────────────────────────────

    async def retrieve(self, state: FilingState) -> FilingState:
        """Pull the most relevant filing chunks for the query.

        Runs before routing so that a resumed (human-approved) run never pays
        for retrieval twice — the checkpointer already holds the context.
        """
        query = state["query"]
        _emit("status", stage="retrieve", message="Searching indexed filings…")

        docs = await self.vector.search(
            query,
            k=4,
            session_id=state.get("session_id"),
        )
        context = "\n\n".join(doc.page_content for doc in docs)[:3000]

        sources = [
            {
                "source": doc.metadata.get("source", "unknown"),
                "page": doc.metadata.get("page"),
                "snippet": doc.page_content[:180].replace("\n", " ").strip(),
            }
            for doc in docs
        ]
        _emit("sources", sources=sources)

        return {"context": context, "sources": sources}

    # ── Routing ──────────────────────────────────────────────────────

    async def router_node(self, state: FilingState) -> FilingState:
        """Classify the query into an analysis category with the LLM."""
        _emit("status", stage="route", message="Classifying query…")

        category = await self.router.classify(state["query"])
        _emit(
            "route",
            category=category,
            label=CATEGORY_LABELS.get(category, category),
            source="auto",
            final=not state.get("require_approval", False),
        )
        return {
            "category": category,
            "proposed_category": category,
            "route_source": "auto",
        }

    def approval(self, state: FilingState) -> FilingState:
        """Pause the run and hand the routing decision to a human.

        ``interrupt()`` suspends the graph here; the checkpointer stores
        everything computed so far. The resume payload may confirm the
        proposed route or swap in a different category, and execution picks up
        from this node — retrieval and classification are not repeated.
        """
        proposed = state.get("category", DEFAULT_CATEGORY)

        decision = interrupt(
            {
                "type": "route_approval",
                "proposed_category": proposed,
                "proposed_label": CATEGORY_LABELS.get(proposed, proposed),
                "query": state.get("query", ""),
                "options": [
                    {"id": c, "label": CATEGORY_LABELS.get(c, c)} for c in CATEGORIES
                ],
            }
        )

        # Accept either {"category": "..."} or a bare category string.
        chosen = proposed
        if isinstance(decision, dict):
            chosen = decision.get("category") or proposed
        elif isinstance(decision, str):
            chosen = decision
        if chosen not in CATEGORIES:
            chosen = proposed

        _emit(
            "route",
            category=chosen,
            label=CATEGORY_LABELS.get(chosen, chosen),
            source="human",
            final=True,
            overridden=chosen != proposed,
        )
        logger.info("Human approved route: %s (proposed %s)", chosen, proposed)
        return {"category": chosen, "route_source": "human"}

    # ── Analysis ─────────────────────────────────────────────────────

    def analysis_node(self, category: str) -> Callable:
        """Build the terminal node that runs `category`-specific analysis.

        Tokens are streamed by LangGraph's ``messages`` stream mode; this node
        only needs to await the full answer for the final state.
        """

        async def node(state: FilingState) -> FilingState:
            _emit(
                "status",
                stage="analyze",
                category=category,
                message=f"Running {CATEGORY_LABELS.get(category, category)} analysis…",
            )
            answer = await self.analysis.analyze(
                category,
                state.get("context", ""),
                state.get("query", ""),
            )
            return {"answer": answer, "category": category}

        node.__name__ = category
        return node
