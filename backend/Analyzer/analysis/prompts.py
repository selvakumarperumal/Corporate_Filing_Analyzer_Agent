"""Prompt loading.

Every prompt lives in ``config/prompts.yaml`` under ``system_prompts``: one
entry per analysis category, plus the router's and the summariser's. The file
is read once at import; :func:`get_prompt` turns a named entry into a LangChain
``ChatPromptTemplate``.
"""

from __future__ import annotations

import logging

import yaml
from langchain_core.prompts import ChatPromptTemplate

from core.paths import PROMPTS_FILE

logger = logging.getLogger(__name__)


def _load_prompts() -> dict[str, str]:
    """Read the system prompts from the YAML config."""
    if not PROMPTS_FILE.exists():
        raise FileNotFoundError(f"Prompts config not found at: {PROMPTS_FILE}")

    config = yaml.safe_load(PROMPTS_FILE.read_text(encoding="utf-8")) or {}
    prompts = config.get("system_prompts") or {}
    if not prompts:
        raise ValueError(f"No 'system_prompts' section in {PROMPTS_FILE}")

    logger.info("Loaded %d prompts from %s", len(prompts), PROMPTS_FILE.name)
    return prompts


_PROMPTS = _load_prompts()


def get_prompt(name: str) -> ChatPromptTemplate:
    """Build the chat prompt template for ``name`` (e.g. ``"router"``, ``"risks"``).

    Raises:
        KeyError: if no prompt with that name exists in the YAML config.
    """
    if name not in _PROMPTS:
        raise KeyError(
            f"Unknown prompt '{name}'. Available: {sorted(_PROMPTS)}"
        )
    return ChatPromptTemplate.from_messages([("system", _PROMPTS[name])])
