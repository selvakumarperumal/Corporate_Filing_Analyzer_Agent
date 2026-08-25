"""Folds the older half of a conversation into a short running summary.

A dossier that has run for fifty questions cannot be sent to the model in full,
and truncating it outright would lose what the analyst established early —
which company, which filing, which period, what has already been ruled out.
Summarising keeps that and drops the wording.

The fold is cumulative: each pass is given the previous summary and only the
turns that have happened since, so the cost of summarising does not grow with
the length of the conversation.
"""

from __future__ import annotations

import logging

from analysis.llm.client import LLMService
from analysis.prompts import get_prompt

logger = logging.getLogger(__name__)

# The summary rides in every subsequent prompt, so it is kept short enough that
# it never becomes the thing crowding the context out.
SUMMARY_CHAR_LIMIT = 1500


class SummaryService:
    """Asks the LLM to compress a stretch of conversation."""

    def __init__(self, llm: LLMService) -> None:
        self.llm = llm
        self.prompt = get_prompt("history_summary")
        logger.info("SummaryService ready")

    async def summarise(self, previous: str, transcript: str) -> str:
        """Fold ``transcript`` into ``previous``, returning the new summary.

        Raises whatever the model call raises: the caller
        (:meth:`conversations.service.HistoryService._summarise`) treats a
        failure as "not summarised yet" and tries again after the next run,
        which is the right outcome — a bad summary would silently distort every
        answer that followed.
        """
        messages = self.prompt.format_messages(
            previous_summary=previous or "(nothing summarised yet)",
            transcript=transcript,
        )
        summary = (await self.llm.ainvoke(messages)).strip()

        if len(summary) > SUMMARY_CHAR_LIMIT:
            summary = summary[:SUMMARY_CHAR_LIMIT].rsplit(" ", 1)[0] + " […]"

        logger.debug("Folded %d chars of transcript into %d", len(transcript), len(summary))
        return summary
