"""Approximate token counting.

Every message is stored with a token count, because context-window management
after the fact is guesswork without one: trimming a history to fit needs to
know what each turn costs, and counting it at write time is the only moment the
text is already in hand.

The count is an estimate, not the tokenizer's answer. The model is served by
Ollama, whose tokenizer is not exposed as a library, and a real count would
mean a round trip per message. Estimating high is the safe direction — a
history trimmed slightly harder than necessary still answers; one trimmed too
softly overflows the window and fails.
"""

from __future__ import annotations

import re

# Tokens are shorter than words and longer than characters. Counting both and
# taking the larger keeps the estimate on the safe side for either extreme:
# dense tables (few words, many symbols) and prose (long words).
_CHARS_PER_TOKEN = 3.6
_TOKENS_PER_WORD = 1.35

_WORDS = re.compile(r"\S+")


def estimate_tokens(text: str) -> int:
    """Roughly how many tokens ``text`` will cost, rounded up."""
    if not text:
        return 0

    by_chars = len(text) / _CHARS_PER_TOKEN
    by_words = len(_WORDS.findall(text)) * _TOKENS_PER_WORD
    return max(1, int(max(by_chars, by_words)) + 1)
