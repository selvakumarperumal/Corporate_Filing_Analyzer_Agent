"""State passed between the nodes of the filing analysis graph."""

from __future__ import annotations

from typing import TypedDict


class FilingState(TypedDict, total=False):
    """Filled in as a run walks ``retrieve -> router -> <category>``.

    ``query`` and ``session_id`` are set at invocation; every other key is
    written by the node that produces it.
    """

    # Inputs
    query: str
    session_id: str

    # What this dossier has already established, as assembled by
    # `services.history_service`: the older turns compressed into `summary`,
    # the recent ones verbatim in `history` as {"role", "content", ...} dicts.
    # Both empty on the first question of a dossier.
    summary: str
    history: list[dict]

    # Passed in carrying the chat's name, or blank for a chat that has none
    # yet — `router` names it on that first run and it comes back in `done`.
    title: str

    # Written by `retrieve`
    context: str

    # Written by `router`
    category: str

    # Written by the analysis node
    answer: str
