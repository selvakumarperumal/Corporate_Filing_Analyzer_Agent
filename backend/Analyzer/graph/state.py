"""Graph state and category definitions for the filing analysis workflow."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

# ── Analysis categories ──────────────────────────────────────────────
# Each category is a terminal node in the graph and maps 1:1 to a prompt
# template in config/prompts.yaml.
CATEGORIES: list[str] = [
    "financials",
    "compliance",
    "risks",
    "shareholding",
    "governance",
    "mda",
    "summary",
    "qa",
]

DEFAULT_CATEGORY = "qa"

# Human-readable labels surfaced in the UI (category picker + route badges).
CATEGORY_LABELS: dict[str, str] = {
    "financials": "Financials",
    "compliance": "Compliance & Audit",
    "risks": "Risk Factors",
    "shareholding": "Shareholding",
    "governance": "Governance",
    "mda": "MD&A",
    "summary": "Executive Summary",
    "qa": "General Q&A",
}

CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "financials": "Revenue, margins, EBITDA, balance sheet and cash flow metrics",
    "compliance": "Auditor opinions, SOX 404 controls, legal and regulatory matters",
    "risks": "Item 1A risk factors, market, credit and cyber vulnerabilities",
    "shareholding": "Ownership structure, institutional holders, promoter stake",
    "governance": "Board composition, committees, executive compensation",
    "mda": "Management Discussion & Analysis, outlook and segment performance",
    "summary": "Executive briefing across the full filing",
    "qa": "Open-ended questions about the filing",
}

RouteSource = Literal["manual", "auto", "human"]
"""How the final category was decided.

- ``manual``: the user picked a category in the UI, the router node is skipped.
- ``auto``: the LLM router classified the query.
- ``human``: the LLM proposed a route and a human approved/overrode it.
"""


class FilingState(TypedDict, total=False):
    """State passed between nodes of the filing analysis graph.

    Written once at invocation (``query``…``require_approval``) and progressively
    enriched by the nodes. Persisted by the checkpointer so an interrupted run
    resumes with retrieval results intact instead of recomputing them.
    """

    # ── Inputs ──
    query: str
    session_id: str
    forced_category: str | None
    require_approval: bool

    # ── Retrieval ──
    context: str
    sources: list[dict[str, Any]]

    # ── Routing ──
    category: str
    route_source: RouteSource
    proposed_category: str

    # ── Output ──
    answer: str
