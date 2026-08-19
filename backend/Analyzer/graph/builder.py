"""Filing analysis graph — routing, human-in-the-loop, and category nodes.

Topology::

                        ┌──────────────► financials ─┐
                        │             ► compliance ─┤
    START ─► retrieve ─►┤ (manual)    ► risks     ──┤
                        │             ► …          ─┼─► END
                        └─► router ─► approval ────┘
                                 │        ▲
                                 └────────┘
                              (skipped when approval is off)

* A category picked in the UI short-circuits the router: the conditional edge
  out of ``retrieve`` jumps straight to that analysis node.
* Otherwise the LLM router classifies, and — when approval is enabled — the
  ``approval`` node interrupts so a human can confirm or override the route.
  The checkpointer lets the run resume at exactly that point.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from graph.nodes import FilingNodes
from graph.state import CATEGORIES, DEFAULT_CATEGORY, FilingState

if TYPE_CHECKING:  # avoids a services ⇄ graph import cycle at runtime
    from services.analysis_service import AnalysisService
    from services.router_service import RouterService
    from services.vector_service import VectorService

logger = logging.getLogger(__name__)

ROUTER_NODE = "router"
APPROVAL_NODE = "approval"
RETRIEVE_NODE = "retrieve"


def _entry_route(state: FilingState) -> str:
    """Send manually-selected categories straight to their analysis node."""
    forced = state.get("forced_category")
    if forced and forced in CATEGORIES:
        return forced
    return ROUTER_NODE


def _after_router(state: FilingState) -> str:
    """Pause for human approval, or go directly to the classified node."""
    if state.get("require_approval"):
        return APPROVAL_NODE
    return state.get("category", DEFAULT_CATEGORY)


def _after_approval(state: FilingState) -> str:
    """Dispatch to whichever category the human settled on."""
    return state.get("category", DEFAULT_CATEGORY)


def build_graph(
    vector: "VectorService",
    router: "RouterService",
    analysis: "AnalysisService",
):
    """Compile the filing analysis graph with an in-memory checkpointer.

    The checkpointer is what makes the human-in-the-loop pause durable: state
    is saved per ``thread_id``, so resuming continues from the interrupt rather
    than restarting the pipeline. Swap ``InMemorySaver`` for a Postgres/SQLite
    saver to survive process restarts.
    """
    nodes = FilingNodes(vector=vector, router=router, analysis=analysis)

    graph = StateGraph(FilingState)
    graph.add_node(RETRIEVE_NODE, nodes.retrieve)
    graph.add_node(ROUTER_NODE, nodes.router_node)
    graph.add_node(APPROVAL_NODE, nodes.approval)
    for category in CATEGORIES:
        graph.add_node(category, nodes.analysis_node(category))

    graph.add_edge(START, RETRIEVE_NODE)

    # retrieve ─► (manual category | router)
    graph.add_conditional_edges(
        RETRIEVE_NODE,
        _entry_route,
        {ROUTER_NODE: ROUTER_NODE, **{c: c for c in CATEGORIES}},
    )

    # router ─► (approval | category)
    graph.add_conditional_edges(
        ROUTER_NODE,
        _after_router,
        {APPROVAL_NODE: APPROVAL_NODE, **{c: c for c in CATEGORIES}},
    )

    # approval ─► category
    graph.add_conditional_edges(
        APPROVAL_NODE,
        _after_approval,
        {c: c for c in CATEGORIES},
    )

    for category in CATEGORIES:
        graph.add_edge(category, END)

    compiled = graph.compile(checkpointer=InMemorySaver())
    logger.info("Filing graph compiled with %d analysis nodes", len(CATEGORIES))
    return compiled
