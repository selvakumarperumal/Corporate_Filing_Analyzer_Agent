"""Prompt templates for Corporate Filing Analyzer Agent.

Loads system prompts from config/prompts.yaml and builds
LangChain ChatPromptTemplate objects for corporate filing analysis.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

import yaml
from langchain_core.prompts import (
    ChatPromptTemplate,
    SystemMessagePromptTemplate,
)

logger = logging.getLogger(__name__)

_PROMPTS_PATH = Path(__file__).resolve().parents[2] / "config" / "prompts.yaml"


def _load_prompts() -> Dict[str, Any]:
    """Load corporate filing prompts YAML configuration once."""
    if not _PROMPTS_PATH.exists():
        raise FileNotFoundError(f"Prompts config not found at: {_PROMPTS_PATH}")

    with open(_PROMPTS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


_PROMPTS: Dict[str, Any] = _load_prompts()


class RouterPrompt:
    """Builds the ChatPromptTemplate for routing queries into filing analysis categories."""

    def __init__(self) -> None:
        system_text = _PROMPTS["system_prompts"]["router"]
        self._system = SystemMessagePromptTemplate.from_template(system_text)
        logger.info("RouterPrompt loaded")

    def get_prompt(self) -> ChatPromptTemplate:
        """Return the chat prompt for query routing."""
        return ChatPromptTemplate.from_messages([self._system])


class QAPrompt:
    """Builds the ChatPromptTemplate for financial analyst Q&A on corporate filing text."""

    def __init__(self) -> None:
        system_text = _PROMPTS["system_prompts"]["qa"]
        self._system = SystemMessagePromptTemplate.from_template(system_text)
        logger.info("QAPrompt loaded")

    def get_prompt(self) -> ChatPromptTemplate:
        """Return the chat prompt for corporate filing Q&A."""
        return ChatPromptTemplate.from_messages([self._system])


class FinancialsPrompt:
    """Builds the ChatPromptTemplate for financial metrics & statement line items extraction."""

    def __init__(self) -> None:
        system_text = _PROMPTS["system_prompts"]["financials"]
        self._system = SystemMessagePromptTemplate.from_template(system_text)
        logger.info("FinancialsPrompt loaded")

    def get_prompt(self) -> ChatPromptTemplate:
        """Return the chat prompt for financial metrics extraction."""
        return ChatPromptTemplate.from_messages([self._system])


class CompliancePrompt:
    """Builds the ChatPromptTemplate for compliance, auditor opinions, and regulatory disclosures."""

    def __init__(self) -> None:
        system_text = _PROMPTS["system_prompts"]["compliance"]
        self._system = SystemMessagePromptTemplate.from_template(system_text)
        logger.info("CompliancePrompt loaded")

    def get_prompt(self) -> ChatPromptTemplate:
        """Return the chat prompt for compliance and audit analysis."""
        return ChatPromptTemplate.from_messages([self._system])


class RisksPrompt:
    """Builds the ChatPromptTemplate for Item 1A risk assessment and classification."""

    def __init__(self) -> None:
        system_text = _PROMPTS["system_prompts"]["risks"]
        self._system = SystemMessagePromptTemplate.from_template(system_text)
        logger.info("RisksPrompt loaded")

    def get_prompt(self) -> ChatPromptTemplate:
        """Return the chat prompt for risk factor analysis."""
        return ChatPromptTemplate.from_messages([self._system])


class SummaryPrompt:
    """Builds the ChatPromptTemplate for executive-level corporate filing summarization."""

    def __init__(self) -> None:
        system_text = _PROMPTS["system_prompts"]["summary"]
        self._system = SystemMessagePromptTemplate.from_template(system_text)
        logger.info("SummaryPrompt loaded")

    def get_prompt(self) -> ChatPromptTemplate:
        """Return the chat prompt for executive filing summarization."""
        return ChatPromptTemplate.from_messages([self._system])


class ShareholdingPrompt:
    """Ownership structure and shareholding pattern extraction."""

    def __init__(self) -> None:
        system_text = _PROMPTS["system_prompts"]["shareholding"]
        self._system = SystemMessagePromptTemplate.from_template(system_text)
        logger.info("ShareholdingPrompt loaded")

    def get_prompt(self) -> ChatPromptTemplate:
        return ChatPromptTemplate.from_messages([self._system])


class GovernancePrompt:
    """Board composition, compensation, and governance details."""

    def __init__(self) -> None:
        system_text = _PROMPTS["system_prompts"]["governance"]
        self._system = SystemMessagePromptTemplate.from_template(system_text)
        logger.info("GovernancePrompt loaded")

    def get_prompt(self) -> ChatPromptTemplate:
        return ChatPromptTemplate.from_messages([self._system])


class MDAPrompt:
    """Management Discussion & Analysis summarization."""

    def __init__(self) -> None:
        system_text = _PROMPTS["system_prompts"]["mda"]
        self._system = SystemMessagePromptTemplate.from_template(system_text)
        logger.info("MDAPrompt loaded")

    def get_prompt(self) -> ChatPromptTemplate:
        return ChatPromptTemplate.from_messages([self._system])
