"""Reading filings and answering questions about them.

The whole model-facing half of the app:

``categories`` / ``prompts``
    what a question can be classified as, and the prompt each one uses.
``llm/``
    the Ollama client and the three things asked of it — classify a question
    and name a dossier, write the answer, fold old turns into a summary.
``retrieval/``
    the vector store the answers are drawn from, one collection per dossier.
``graph/``
    the LangGraph run that puts those in order.
``pipeline``
    the one object the rest of the app talks to.
"""

from analysis.categories import (
    CATEGORIES,
    CATEGORY_LABELS,
    DEFAULT_CATEGORY,
    label_of,
)
from analysis.pipeline import AnalysisPipeline, scoped_session_id
from analysis.prompts import get_prompt

__all__ = [
    "CATEGORIES",
    "CATEGORY_LABELS",
    "DEFAULT_CATEGORY",
    "AnalysisPipeline",
    "get_prompt",
    "label_of",
    "scoped_session_id",
]
