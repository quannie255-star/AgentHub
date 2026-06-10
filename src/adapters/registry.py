"""Adapter registry — central lookup for all registered agent adapters.

The ``AdapterRegistry`` is the single source of truth that the
``AgentRouter`` and ``Orchestrator`` use to discover available agents.

Usage::

    registry = AdapterRegistry()
    registry.register(ClaudeCodeAdapter(cli_path="claude"))
    registry.register(CodexCLIAdapter())

    agent = registry.get("claude")
    caps = agent.get_capabilities()

    all_agents = registry.list_agents()
"""

from __future__ import annotations

import asyncio

from src.adapters.base import AbstractAgentAdapter, AgentUnavailableError
from src.core.schema import AgentCapability, AgentStatus


class AdapterRegistryError(Exception):
    """Raised on registry-level errors (duplicate, not found)."""


class AdapterRegistry:
    """Thread-safe registry of agent adapters.

    Agents are keyed by ``adapter.agent_name``.  Duplicate registrations
    are rejected by default (use ``replace=True`` to override).
    """

    def __init__(self) -> None:
        self._adapters: dict[str, AbstractAgentAdapter] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    async def register(
        self, adapter: AbstractAgentAdapter, *, replace: bool = False
    ) -> None:
        """Register an adapter.

        Args:
            adapter: The adapter instance to register.
            replace: If True, overwrite an existing adapter with the same name.

        Raises:
            AdapterRegistryError: If an adapter with the same name is already
                registered and ``replace`` is False.
        """
        name = adapter.agent_name
        async with self._lock:
            if name in self._adapters and not replace:
                raise AdapterRegistryError(
                    f"Agent '{name}' is already registered. Use replace=True to override."
                )
            self._adapters[name] = adapter

    async def unregister(self, agent_name: str) -> None:
        """Remove an adapter from the registry.

        No-op if the agent is not registered.
        """
        async with self._lock:
            self._adapters.pop(agent_name, None)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    async def get(self, agent_name: str) -> AbstractAgentAdapter:
        """Return the adapter for ``agent_name``.

        Raises:
            AdapterRegistryError: If the agent is not registered.
        """
        async with self._lock:
            if agent_name not in self._adapters:
                raise AdapterRegistryError(f"Agent '{agent_name}' not found in registry")
            return self._adapters[agent_name]

    async def get_or_none(self, agent_name: str) -> AbstractAgentAdapter | None:
        """Return the adapter or None if not registered."""
        async with self._lock:
            return self._adapters.get(agent_name)

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    async def list_agents(self) -> list[str]:
        """Return all registered agent names."""
        async with self._lock:
            return list(self._adapters.keys())

    async def list_capabilities(self) -> list[AgentCapability]:
        """Return capability profiles for all registered agents."""
        async with self._lock:
            return [a.get_capabilities() for a in self._adapters.values()]

    async def find_by_action(self, action: str) -> list[str]:
        """Return agent names that support a given action.

        Example:
            >>> agents = await registry.find_by_action("code_generation")
            >>> "claude" in agents
            True
        """
        matching: list[str] = []
        async with self._lock:
            for name, adapter in self._adapters.items():
                caps = adapter.get_capabilities()
                if action in caps.supported_actions:
                    matching.append(name)
        return matching

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health_check_all(self) -> dict[str, AgentStatus]:
        """Run ``health_check()`` on all registered agents concurrently."""
        async with self._lock:
            items = list(self._adapters.items())

        results = {}
        tasks = {}
        for name, adapter in items:
            tasks[name] = asyncio.create_task(
                self._safe_health_check(name, adapter)
            )
        for name, task in tasks.items():
            results[name] = await task
        return results

    async def _safe_health_check(
        self, name: str, adapter: AbstractAgentAdapter
    ) -> AgentStatus:
        try:
            return await adapter.health_check()
        except Exception:
            return AgentStatus.ERROR
