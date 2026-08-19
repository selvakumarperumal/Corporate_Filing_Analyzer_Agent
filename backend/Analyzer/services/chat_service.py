"""Chat service — drives the LangGraph filing analysis workflow.

Owns the compiled graph and translates its multi-mode stream into flat
Socket.IO-ready events. Every turn gets its own graph thread so an
interrupted run can be resumed independently of any other in-flight turn.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, AsyncIterator

from langgraph.types import Command

from graph.builder import build_graph
from graph.state import CATEGORIES, CATEGORY_LABELS, DEFAULT_CATEGORY
from services.analysis_service import AnalysisService
from services.llm_service import LLMService
from services.router_service import RouterService
from services.vector_service import VectorService

logger = logging.getLogger(__name__)

_STREAM_MODES = ["custom", "messages", "updates"]


class ChatService:
    """Main orchestrator for the corporate filing chat pipeline.

    Flow (see :mod:`graph.builder` for the graph topology):
        1. ``retrieve``  — fetch relevant chunks for the query
        2. ``router``    — LLM classification, skipped if the UI forced a category
        3. ``approval``  — optional human-in-the-loop pause on the routing decision
        4. ``<category>``— category-specific analysis, streamed token by token
    """

    def __init__(
        self,
        llm: LLMService,
        vector: VectorService,
        router: RouterService,
        analysis: AnalysisService,
    ) -> None:
        self.llm = llm
        self.vector = vector
        self.router = router
        self.analysis = analysis
        self.graph = build_graph(vector=vector, router=router, analysis=analysis)
        logger.info("ChatService initialized (LangGraph workflow)")

    @classmethod
    def from_defaults(
        cls,
        model: str = "llama3.1:latest",
        embedding_model: str = "nomic-embed-text:latest",
        base_url: str = "http://localhost:11434",
    ) -> ChatService:
        """Create ChatService with default configuration."""
        llm = LLMService(
            model=model,
            embedding_model=embedding_model,
            base_url=base_url,
        )
        vector = VectorService(embeddings=llm.embeddings)
        router = RouterService(llm=llm)
        analysis = AnalysisService(llm=llm)
        return cls(llm=llm, vector=vector, router=router, analysis=analysis)

    # ── Ingestion ────────────────────────────────────────────────────

    async def upload_file(
        self,
        file_bytes: bytes,
        filename: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Ingest an uploaded file into the vector store for a chat session."""
        chunk_count = await self.vector.ingest_file(file_bytes, filename, session_id)
        return {
            "status": "ok",
            "filename": filename,
            "chunks_ingested": chunk_count,
            "session_id": session_id,
        }

    # ── Graph execution ──────────────────────────────────────────────

    async def query_stream(
        self,
        user_query: str,
        session_id: str | None = None,
        category: str | None = None,
        require_approval: bool = False,
        run_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Run a query through the graph, yielding events as they occur.

        Args:
            user_query: The analyst's question.
            session_id: Chat session, used to scope retrieval to its uploads.
            category: A category chosen in the UI. When set, the router node is
                bypassed entirely and the graph jumps straight to that node.
            require_approval: Pause on the auto-routing decision for human
                confirmation before running the analysis.
            run_id: Graph thread id; generated when omitted.

        Yields dicts suitable for Socket.IO emission, e.g.::

            {"event": "status",    "stage": "retrieve", ...}
            {"event": "route",     "category": "risks", "source": "auto"}
            {"event": "interrupt", "run_id": "...", "proposed_category": "..."}
            {"event": "token",     "content": "..."}
            {"event": "done",      "category": "risks"}
        """
        run_id = run_id or uuid.uuid4().hex
        forced = category if category in CATEGORIES else None

        payload = {
            "query": user_query,
            "session_id": session_id or "",
            "forced_category": forced,
            # A manually picked category needs no approval — the human already chose.
            "require_approval": bool(require_approval) and forced is None,
            "category": forced or DEFAULT_CATEGORY,
            "route_source": "manual" if forced else "auto",
        }

        yield {"event": "run_started", "run_id": run_id}
        if forced:
            yield {
                "event": "route",
                "category": forced,
                "label": CATEGORY_LABELS.get(forced, forced),
                "source": "manual",
                "final": True,
            }

        async for event in self._consume(payload, run_id):
            yield event

    async def resume_stream(
        self,
        run_id: str,
        category: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Resume an interrupted run from its approval pause.

        The graph continues at the ``approval`` node with everything computed
        before the pause still in state — retrieval and classification are not
        repeated.
        """
        resume_value: dict[str, Any] = {}
        if category in CATEGORIES:
            resume_value["category"] = category

        yield {"event": "resumed", "run_id": run_id}
        async for event in self._consume(Command(resume=resume_value), run_id):
            yield event

    # ── Internals ────────────────────────────────────────────────────

    async def _consume(
        self,
        graph_input: Any,
        run_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Flatten the graph's multi-mode stream into UI events."""
        config = {"configurable": {"thread_id": run_id}}
        interrupted = False
        category = DEFAULT_CATEGORY

        async for mode, chunk in self.graph.astream(
            graph_input,
            config=config,
            stream_mode=_STREAM_MODES,
        ):
            if mode == "custom":
                if chunk.get("event") == "route":
                    category = chunk.get("category", category)
                yield chunk

            elif mode == "messages":
                message, metadata = chunk
                # Only analysis nodes stream to the user; the router's own
                # LLM call runs through the same stream and must be dropped.
                if metadata.get("langgraph_node") not in CATEGORIES:
                    continue
                content = getattr(message, "content", "")
                if content:
                    yield {"event": "token", "content": content}

            elif mode == "updates":
                if "__interrupt__" in chunk:
                    interrupted = True
                    for item in chunk["__interrupt__"]:
                        yield {
                            "event": "interrupt",
                            "run_id": run_id,
                            **(item.value if isinstance(item.value, dict) else {}),
                        }
                    continue
                for node_name, update in chunk.items():
                    if node_name in CATEGORIES:
                        category = node_name

        if interrupted:
            logger.info("Run %s paused for human approval", run_id)
            return

        yield {"event": "done", "run_id": run_id, "category": category}
        logger.info("Run %s completed (category=%s)", run_id, category)
