"""Classifies a user question into one analysis category, and names the chat."""

from __future__ import annotations

import logging
import re

from core.categories import CATEGORIES, DEFAULT_CATEGORY
from prompts.templates import get_prompt
from services.llm_service import LLMService

logger = logging.getLogger(__name__)

# A chat name has to survive in a narrow sidebar, so it is clipped rather than
# left to run past the edge.
TITLE_CHAR_LIMIT = 42
FALLBACK_TITLE = "Untitled dossier"

# Small models like to answer a "give me a title" prompt with `Title: "..."`,
# so the lead-in and the wrapping punctuation are both stripped off.
_TITLE_LEAD_IN = re.compile(r"^\s*(?:title|dossier|name)\s*[:\-]\s*", re.IGNORECASE)
_TITLE_TRIM = " \t\"'`*_.,;:-—"

# Prompts are sent as a single system message, and a chat template given no user
# turn can echo its role header back as the first line of the reply — llama3.1
# opens with a bare "assistant". The name is the first line that is not one.
_ROLE_LINE = re.compile(r"^(?:assistant|user|system|model)\b[:,]?$", re.IGNORECASE)


class RouterService:
    """Asks the LLM which analysis category a question belongs to."""

    def __init__(self, llm: LLMService) -> None:
        self.llm = llm
        self.prompt = get_prompt("router")
        self.title_prompt = get_prompt("title")
        logger.info("RouterService ready (%d categories)", len(CATEGORIES))

    async def classify(self, query: str) -> str:
        """Return the category for ``query``, or the default if unrecognised."""
        messages = self.prompt.format_messages(query=query)
        response = await self.llm.ainvoke(messages)

        # The model is asked for a bare category name, but small models like to
        # add punctuation or a sentence around it — take the first word that is
        # a known category.
        words = re.sub(r"[^a-zA-Z\s]", " ", response).lower().split()
        category = next((word for word in words if word in CATEGORIES), None)

        if category is None:
            logger.warning(
                "Router reply %r matched no category — using '%s'",
                response.strip()[:120],
                DEFAULT_CATEGORY,
            )
            category = DEFAULT_CATEGORY

        logger.info("Routed %r -> %s", query[:60], category)
        logger.debug("Router raw reply: %r", response.strip())
        return category

    async def name_chat(self, query: str) -> str:
        """Name a chat after the question that opened it.

        Called once per chat, on its first analysed run. Never raises: a chat
        that cannot be named still has to get its answer, so a model that
        replies with nothing usable falls back to a placeholder name.
        """
        try:
            messages = self.title_prompt.format_messages(query=query)
            response = await self.llm.ainvoke(messages)
        except Exception:
            logger.exception("Naming the chat failed — using '%s'", FALLBACK_TITLE)
            return FALLBACK_TITLE

        title = _clean_title(response)
        if not title:
            logger.warning(
                "Model returned no usable title (%r) — using '%s'",
                response.strip()[:120],
                FALLBACK_TITLE,
            )
            return FALLBACK_TITLE

        logger.info("Named %r -> %r", query[:60], title)
        return title


def _clean_title(raw: str) -> str:
    """Reduce a model's reply to the bare title, clipped to the sidebar's width.

    Returns an empty string when nothing usable is left.
    """
    # Only one line is the name: anything after it is the model explaining
    # itself, and anything before it is chat-template noise.
    lines = (line.strip() for line in raw.splitlines() if line.strip())
    line = next((line for line in lines if not _ROLE_LINE.match(line)), "")
    line = _TITLE_LEAD_IN.sub("", line)
    line = re.sub(r"\s+", " ", line).strip(_TITLE_TRIM)

    if len(line) > TITLE_CHAR_LIMIT:
        # Clip on a word boundary, falling back to a hard cut for a single word
        # longer than the limit.
        clipped = line[:TITLE_CHAR_LIMIT].rsplit(" ", 1)[0] or line[:TITLE_CHAR_LIMIT]
        line = clipped.strip(_TITLE_TRIM) + "…"

    return line
