"""Analysis service for category-specific filing analysis with streaming."""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from langchain_core.messages import HumanMessage
from langchain_core.prompts import ChatPromptTemplate

from prompts.templates import (
    QAPrompt,
    FinancialsPrompt,
    CompliancePrompt,
    RisksPrompt,
    ShareholdingPrompt,
    GovernancePrompt,
    MDAPrompt,
    SummaryPrompt,
)
from services.llm_service import LLMService

logger = logging.getLogger(__name__)


class AnalysisService:
    """Runs category-specific analysis on corporate filing context."""

    def __init__(self, llm: LLMService) -> None:
        self.llm = llm
        self._prompts: dict[str, ChatPromptTemplate] = {
            "qa": QAPrompt().get_prompt(),
            "financials": FinancialsPrompt().get_prompt(),
            "compliance": CompliancePrompt().get_prompt(),
            "risks": RisksPrompt().get_prompt(),
            "shareholding": ShareholdingPrompt().get_prompt(),
            "governance": GovernancePrompt().get_prompt(),
            "mda": MDAPrompt().get_prompt(),
            "summary": SummaryPrompt().get_prompt(),
        }
        logger.info("AnalysisService initialized with %d categories", len(self._prompts))

    async def analyze(self, category: str, context: str, query: str = "") -> str:
        """Run analysis and return full response."""
        messages = self._build_messages(category, context, query)
        return await self.llm.ainvoke(messages)

    async def analyze_stream(
        self, category: str, context: str, query: str = ""
    ) -> AsyncIterator[str]:
        """Run analysis and stream response tokens."""
        messages = self._build_messages(category, context, query)
        async for token in self.llm.astream(messages):
            yield token

    def _build_messages(self, category: str, context: str, query: str = "") -> list:
        """Build prompt messages for the given category."""
        if category not in self._prompts:
            raise ValueError(f"Unknown category: {category}")

        prompt = self._prompts[category]
        input_vars = prompt.input_variables

        kwargs: dict[str, str] = {"context": context}
        if "query" in input_vars:
            kwargs["query"] = query

        messages = prompt.format_messages(**kwargs)

        # Report-style prompts take only {context}. Append the analyst's own
        # question so a manually routed run still answers what was asked.
        if query and "query" not in input_vars:
            messages.append(
                HumanMessage(
                    content=(
                        f"Analyst request: {query}\n\n"
                        "Produce the report above, giving extra emphasis to this request."
                    )
                )
            )

        return messages
