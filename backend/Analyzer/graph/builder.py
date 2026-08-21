"""Builds the filing analysis graph.

Topology::

                                     +-> financials -+
    START -> retrieve -+-> router ---+-> risks -------+-> END
                       |             +-> ... 6 more --+
                       +-> no_filing -------------------> END

Retrieve the chat's filing text, let the LLM router classify the question, then
dispatch to the matching analysis node. A chat with no filing indexed skips
straight to ``no_filing`` — running an analysis prompt on an empty context just
makes the model invent a report.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langgraph.graph import END, START, StateGraph

from core.categories import CATEGORIES, DEFAULT_CATEGORY
from graph.nodes import FilingNodes
from graph.state import FilingState

if TYPE_CHECKING:  # avoids a services <-> graph import cycle at runtime
    from services.analysis_service import AnalysisService
    from services.router_service import RouterService
    from services.vector_service import VectorService

logger = logging.getLogger(__name__)

RETRIEVE_NODE = "retrieve"
ROUTER_NODE = "router"
NO_FILING_NODE = "no_filing"


def _after_retrieve(state: FilingState) -> str:
    """Skip the analysis entirely when this chat has no filing to read."""
    return ROUTER_NODE if state.get("context") else NO_FILING_NODE


def _after_router(state: FilingState) -> str:
    """Pick the analysis node matching the category the router chose."""
    return state.get("category", DEFAULT_CATEGORY)


def build_graph(
    vector: VectorService,
    router: RouterService,
    analysis: AnalysisService,
):
    """Compile the filing analysis graph.

    No checkpointer: a run goes straight through with nothing to pause on, so
    there is no cross-turn state worth persisting.
    """
    nodes = FilingNodes(vector=vector, router=router, analysis=analysis)

    graph = StateGraph(FilingState)
    graph.add_node(RETRIEVE_NODE, nodes.retrieve)
    graph.add_node(ROUTER_NODE, nodes.router_node)
    graph.add_node(NO_FILING_NODE, nodes.no_filing)
    for category in CATEGORIES:
        graph.add_node(category, nodes.analysis_node(category))

    graph.add_edge(START, RETRIEVE_NODE)
    graph.add_conditional_edges(
        RETRIEVE_NODE,
        _after_retrieve,
        {ROUTER_NODE: ROUTER_NODE, NO_FILING_NODE: NO_FILING_NODE},
    )
    graph.add_conditional_edges(ROUTER_NODE, _after_router, {c: c for c in CATEGORIES})

    graph.add_edge(NO_FILING_NODE, END)
    for category in CATEGORIES:
        graph.add_edge(category, END)

    logger.info("Filing graph compiled with %d analysis nodes", len(CATEGORIES))
    return graph.compile()
