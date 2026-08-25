"""What the language model is asked to do, one module per job.

``client``      the Ollama chat model and embeddings
``routing``     classify a question into a category; name a new dossier
``analyst``     write the answer for a category, given the retrieved filing
``summarizer``  fold the older turns of a dossier into a rolling summary
"""

from analysis.llm.analyst import AnalysisService
from analysis.llm.client import LLMService
from analysis.llm.routing import RouterService
from analysis.llm.summarizer import SummaryService

__all__ = ["AnalysisService", "LLMService", "RouterService", "SummaryService"]
