"""Ollama chat model and embeddings."""

from __future__ import annotations

import logging

from langchain_core.messages import BaseMessage
from langchain_ollama import ChatOllama, OllamaEmbeddings

logger = logging.getLogger(__name__)


class LLMService:
    """Wraps the Ollama chat model and the embedding model."""

    def __init__(
        self,
        model: str,
        embedding_model: str,
        base_url: str,
        temperature: float = 0.0,
    ) -> None:
        self.chat_model = ChatOllama(
            model=model,
            base_url=base_url,
            temperature=temperature,
        )
        self.embeddings = OllamaEmbeddings(model=embedding_model, base_url=base_url)
        logger.info(
            "LLMService ready (model=%s, embeddings=%s, base_url=%s, temperature=%s)",
            model,
            embedding_model,
            base_url,
            temperature,
        )

    async def ainvoke(self, messages: list[BaseMessage]) -> str:
        """Send messages to the model and return the reply as text."""
        logger.debug("LLM call with %d message(s)", len(messages))
        response = await self.chat_model.ainvoke(messages)
        # `.text` flattens both plain-string and content-block replies.
        text = str(response.text)
        logger.debug("LLM replied with %d characters", len(text))
        return text
