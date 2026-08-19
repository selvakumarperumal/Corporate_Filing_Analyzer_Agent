"""Ollama LLM service for Corporate Filing Analyzer."""

from __future__ import annotations

import logging
from typing import AsyncIterator

from langchain_ollama import ChatOllama, OllamaEmbeddings
from langchain_core.messages import BaseMessage

logger = logging.getLogger(__name__)


class LLMService:
    """Wraps Ollama chat model and embeddings."""

    def __init__(
        self,
        model: str = "llama3.1:latest",
        embedding_model: str = "nomic-embed-text:latest",
        base_url: str = "http://localhost:11434",
        temperature: float = 0.0,
    ) -> None:
        self.chat_model = ChatOllama(
            model=model,
            base_url=base_url,
            temperature=temperature,
        )
        self.embeddings = OllamaEmbeddings(
            model=embedding_model,
            base_url=base_url,
        )
        logger.info(
            "LLMService initialized (model=%s, embeddings=%s)",
            model,
            embedding_model,
        )

    async def ainvoke(self, messages: list[BaseMessage]) -> str:
        """Invoke LLM and return full response content."""
        response = await self.chat_model.ainvoke(messages)
        return response.content

    async def astream(self, messages: list[BaseMessage]) -> AsyncIterator[str]:
        """Stream LLM response token by token."""
        async for chunk in self.chat_model.astream(messages):
            if chunk.content:
                yield chunk.content
