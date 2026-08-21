"""The analysis categories the router can classify a question into.

One category = one prompt in ``config/prompts.yaml`` = one node in the graph.
Kept here (and nowhere else) so the router, the graph and the prompts can never
drift out of sync.
"""

from __future__ import annotations

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

# Used when the router's answer doesn't match any known category.
DEFAULT_CATEGORY = "qa"

# Human-readable names, used in log lines.
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


def label_of(category: str) -> str:
    """Human-readable name for a category."""
    return CATEGORY_LABELS.get(category, category)
