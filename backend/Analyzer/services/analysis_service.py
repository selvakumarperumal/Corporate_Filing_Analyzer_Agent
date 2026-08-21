"""Runs category-specific analysis over retrieved filing text."""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage, SystemMessage

from core.categories import CATEGORIES
from prompts.templates import get_prompt
from services.llm_service import LLMService

logger = logging.getLogger(__name__)

# How much of one earlier message is repeated back into the prompt. The history
# is already under a token budget by the time it arrives; this stops a single
# long report inside that budget from crowding out the turns around it, so the
# model sees the shape of the conversation rather than one wall of it.
HISTORY_CHARS_PER_MESSAGE = 800

_SPEAKER = {"user": "Analyst", "assistant": "Analyzer"}


class AnalysisService:
    """Builds the prompt for a category and asks the LLM for the answer."""

    def __init__(self, llm: LLMService) -> None:
        self.llm = llm
        self.prompts = {name: get_prompt(name) for name in CATEGORIES}
        logger.info("AnalysisService ready (%d categories)", len(self.prompts))

    async def analyze(
        self,
        category: str,
        context: str,
        query: str,
        summary: str = "",
        history: list[dict] | None = None,
    ) -> str:
        """Answer ``query`` about ``context`` using the ``category`` prompt.

        ``summary`` and ``history`` are what the conversation has established
        so far — the rolling summary of the older turns, and the recent ones
        verbatim. Both are optional: the first question in a dossier has
        neither, and the prompt is then exactly what it always was.
        """
        messages = self._build_messages(category, context, query, summary, history or [])
        logger.debug(
            "Analyzing category=%s (context=%d chars, history=%d msg, query=%r)",
            category,
            len(context),
            len(history or []),
            query[:60],
        )
        answer = await self.llm.ainvoke(messages)
        logger.info("Analysis complete: category=%s, answer=%d chars", category, len(answer))
        return answer

    def _build_messages(
        self,
        category: str,
        context: str,
        query: str,
        summary: str = "",
        history: list[dict] | None = None,
    ) -> list:
        """Fill the category prompt with the retrieved context and question."""
        if category not in self.prompts:
            raise ValueError(f"Unknown category: {category}")

        prompt = self.prompts[category]
        variables = {"context": context}
        if "query" in prompt.input_variables:
            variables["query"] = query

        messages = prompt.format_messages(**variables)

        # Straight after the instructions and before the question, so the model
        # reads the conversation as background to the task rather than as part
        # of the filing it was told to answer from.
        earlier = self._history_message(summary, history or [])
        if earlier is not None:
            messages.append(earlier)

        # The report prompts take only {context}. Append the question so the
        # report still leans towards what was actually asked.
        if query and "query" not in prompt.input_variables:
            messages.append(
                HumanMessage(
                    content=(
                        f"Analyst request: {query}\n\n"
                        "Produce the report above, giving extra emphasis to this request."
                    )
                )
            )
        return messages

    def _history_message(self, summary: str, history: list[dict]) -> SystemMessage | None:
        """The conversation so far, as one block, or ``None`` if there is none.

        One system message rather than a run of alternating human/assistant
        turns: half these prompts already carry the question inside the system
        message, so replaying the history as real turns would land it *after*
        the question in some categories and before it in others. A single
        labelled block reads the same way in both.
        """
        if not summary and not history:
            return None

        parts = ["Earlier in this dossier (background — the filing above is the source of fact):"]
        if summary:
            parts.append(f"\nSummary of earlier exchanges:\n{summary}")

        if history:
            lines = []
            for message in history:
                content = (message.get("content") or "").strip()
                if len(content) > HISTORY_CHARS_PER_MESSAGE:
                    content = content[:HISTORY_CHARS_PER_MESSAGE].rstrip() + " […]"
                speaker = _SPEAKER.get(message.get("role", ""), "Analyst")
                lines.append(f"{speaker}: {content}")
            parts.append("\nRecent exchanges:\n" + "\n\n".join(lines))

        parts.append(
            "\nUse this only to resolve what the analyst is referring to and to "
            "avoid repeating yourself. Do not treat it as evidence about the filing."
        )
        return SystemMessage(content="\n".join(parts))
