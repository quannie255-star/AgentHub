"""Abstract base class for all Agent adapters.

Every agent platform (Claude Code, Codex CLI, custom agents) must implement
this interface. The orchestrator and message bus depend ONLY on this ABC —
never on concrete adapter types.

Key contract:
  - ``stream_response()`` MUST return ``AsyncIterator[str]`` — no other type.
  - ``send_message()`` returns a complete ``AgentResponse``.
  - ``get_capabilities()`` is used by the AgentRouter for task assignment.
  - ``cancel()`` must be safe to call at any time (idempotent).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from src.core.schema import AgentCapability, AgentContext, AgentResponse, AgentStatus


class AgentAdapterError(Exception):
    """Base exception for adapter-level failures."""


class AgentTimeoutError(AgentAdapterError):
    """Raised when an agent exceeds its configured timeout."""


class AgentUnavailableError(AgentAdapterError):
    """Raised when an agent is not reachable (CLI missing, API down, etc.)."""


# ======================================================================
# Abstract Base Class
# ======================================================================

class AbstractAgentAdapter(ABC):
    """Uniform interface for all agent backends.

    Subclasses MUST implement all ``@abstractmethod`` decorated methods.
    """

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def agent_name(self) -> str:
        """Unique identifier for this agent (e.g. 'claude', 'codex')."""
        ...

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    @abstractmethod
    async def send_message(self, msg: str, context: AgentContext) -> AgentResponse:
        """Send a message and wait for the complete response.

        Args:
            msg: The user message text (may include @-mentions stripped by caller).
            context: Session context (history, system prompt, etc.).

        Returns:
            AgentResponse with full content, token usage, and finish reason.

        Raises:
            AgentTimeoutError: If the agent exceeds the configured timeout.
            AgentUnavailableError: If the agent backend is unreachable.
        """
        ...

    @abstractmethod
    async def stream_response(self, msg: str, context: AgentContext) -> AsyncIterator[str]:
        """Stream the agent response token-by-token (or chunk-by-chunk).

        **Return type is enforced as ``AsyncIterator[str]``** — subclasses
        cannot return a different type without violating the ABC contract.

        Usage::

            async for chunk in adapter.stream_response(msg, ctx):
                yield f"data: {chunk}\\n\\n"  # SSE to frontend

        Args:
            msg: The user message text.
            context: Session context.

        Yields:
            String chunks of the agent's response.

        Raises:
            AgentTimeoutError: On timeout.
            AgentUnavailableError: If backend is unreachable.
        """
        ...

    # ------------------------------------------------------------------
    # Capabilities & Health
    # ------------------------------------------------------------------

    @abstractmethod
    def get_capabilities(self) -> AgentCapability:
        """Return the agent's capability profile.

        Used by ``AgentRouter`` to decide which agent to assign a task to.
        """
        ...

    @abstractmethod
    async def health_check(self) -> AgentStatus:
        """Check whether the agent backend is reachable and ready.

        Returns:
            ``AgentStatus.IDLE`` if ready, ``AgentStatus.OFFLINE`` if unreachable,
            ``AgentStatus.ERROR`` if reachable but broken.
        """
        ...

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abstractmethod
    async def cancel(self) -> None:
        """Cancel the currently running request (if any).

        Must be idempotent — safe to call when no request is in flight.
        """
        ...

    async def close(self) -> None:
        """Release any resources held by the adapter.

        Default implementation calls ``cancel()`` and does nothing else.
        Override to clean up subprocess pools, HTTP sessions, etc.
        """
        await self.cancel()
