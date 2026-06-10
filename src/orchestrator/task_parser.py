"""Task Parser — extract @-mentions and decompose user requests into sub-tasks.

Two responsibilities:
  1. **Mention extraction**: deterministic regex to find ``@agent_name`` patterns.
  2. **Task decomposition**: break a natural-language request into a list of
     ``SubTask`` objects.  Supports a fast regex-based path (numbered/comma items)
     and an optional LLM-based path for complex requests.

The parser produces ``SubTask`` instances that the ``AgentRouter`` later
assigns to specific agents.
"""

from __future__ import annotations

import re
from typing import Callable, Awaitable

from src.core.schema import SubTask


# Regex patterns
_MENTION_RE = re.compile(r"@([\w_-]+)")
_LIST_ITEM_RE = re.compile(r"(?:^|\n)\s*(?:\d+[.、)]|[-*])\s*(.+)", re.MULTILINE)
_COMMA_SPLIT_RE = re.compile(r"[,，;；]\s*")


def extract_mentions(text: str) -> list[str]:
    """Extract unique @-mentioned agent names from text.

    Examples:
        >>> extract_mentions("@claude fix the login page @codex review")
        ['claude', 'codex']
        >>> extract_mentions("just a plain message")
        []
    """
    return list(dict.fromkeys(_MENTION_RE.findall(text)))  # dedup, preserve order


def strip_mentions(text: str) -> str:
    """Remove all @-mentions from the text, preserving newlines.

    Example:
        >>> strip_mentions("@claude fix this @codex")
        'fix this'
    """
    cleaned = _MENTION_RE.sub("", text)
    # Collapse multiple spaces/tabs but preserve newlines
    lines = cleaned.split("\n")
    lines = [" ".join(line.split()) for line in lines]
    return "\n".join(line for line in lines if line).strip()


# ---------------------------------------------------------------------------
# Decomposition strategies
# ---------------------------------------------------------------------------

def decompose_numbered(text: str) -> list[str]:
    """Split text by numbered/bulleted list items.

    Falls back to the full text if no list structure is detected.

    Example:
        >>> decompose_numbered("1. add login\\\\n2. add dashboard")
        ['add login', 'add dashboard']
    """
    matches = _LIST_ITEM_RE.findall(text)
    if matches:
        return [m.strip() for m in matches]
    return [text.strip()]


def decompose_comma(text: str) -> list[str]:
    """Split text by commas / semicolons when it looks like a flat list.

    Example:
        >>> decompose_comma("fix bug A, add feature B, write test C")
        ['fix bug A', 'add feature B', 'write test C']
    """
    parts = _COMMA_SPLIT_RE.split(text)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) > 1:
        return parts
    return [text.strip()]


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

class TaskParser:
    """Parses user messages into sub-tasks.

    Args:
        llm_decompose: Optional async callable ``(text: str) -> list[str]``
            that uses an LLM to decompose complex requests.  If omitted,
            only regex-based decomposition is used.
    """

    def __init__(
        self,
        llm_decompose: Callable[[str], Awaitable[list[str]]] | None = None,
    ):
        self._llm_decompose = llm_decompose

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def parse(self, text: str) -> list[SubTask]:
        """Parse user text into a list of sub-tasks.

        Steps:
          1. Extract @-mentions.
          2. Strip mentions from the text.
          3. Decompose remaining text into sub-task descriptions.
          4. Wrap each description in a ``SubTask``.

        Args:
            text: Raw user message (may contain @-mentions).

        Returns:
            List of SubTask objects (dependencies empty — filled by AgentRouter).
        """
        clean = strip_mentions(text)
        descriptions = await self._decompose(clean)
        return [
            SubTask(description=d) for d in descriptions if d
        ]

    # ------------------------------------------------------------------
    # Internal decomposition pipeline
    # ------------------------------------------------------------------

    async def _decompose(self, text: str) -> list[str]:
        """Try strategies in order: numbered → comma → LLM → fallback."""
        # Strategy 1: numbered / bulleted list
        result = decompose_numbered(text)
        if len(result) > 1:
            return result

        # Strategy 2: comma / semicolon list
        result = decompose_comma(text)
        if len(result) > 1:
            return result

        # Strategy 3: LLM decomposition (if available)
        if self._llm_decompose:
            try:
                return await self._llm_decompose(text)
            except Exception:
                pass  # Fall through to fallback

        # Strategy 4: single-item fallback
        return [text.strip()]
