"""Router service for query classification."""

from __future__ import annotations

import logging
import re
from typing import Any

from prompts.templates import RouterPrompt
from services.llm_service import LLMService

logger = logging.getLogger(__name__)

VALID_CATEGORIES = [
    "financials",
    "compliance",
    "risks",
    "shareholding",
    "governance",
    "mda",
    "summary",
    "qa",
]


class RouterService:
    """Classifies user queries into analysis categories using Ollama."""

    def __init__(self, llm: LLMService) -> None:
        self.llm = llm
        self.prompt = RouterPrompt().get_prompt()
        logger.info("RouterService initialized")

    async def classify(self, query: str) -> str:
        """Classify query into one of the filing analysis categories."""
        messages = self.prompt.format_messages(query=query)
        response = await self.llm.ainvoke(messages)
        
        # Clean response and match against known categories
        clean_tokens = re.sub(r"[^a-zA-Z\s]", " ", response).lower().split()
        matched_category = "qa"

        for token in clean_tokens:
            if token in VALID_CATEGORIES:
                matched_category = token
                break

        logger.info("Query '%s' classified as '%s' (raw response: %r)", query[:60], matched_category, response.strip())
        return matched_category
