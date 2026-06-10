"""Agent Router — assign sub-tasks to agents with retry/fallback logic.

The router uses ``AdapterRegistry`` to match sub-task descriptions to
agent capabilities.  It supports:

  - **Primary assignment**: best-matching agent by action keywords.
  - **Retry**: retry failed sub-tasks up to ``max_retries`` times.
  - **Fallback**: if primary agent exhausts retries, reassign to
    ``fallback_agent`` (from ``OrchestrationTask``).
  - **Timeout**: per-sub-task timeout enforced by the orchestrator.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from src.adapters.base import AbstractAgentAdapter, AgentAdapterError
from src.adapters.registry import AdapterRegistry
from src.core.schema import (
    AgentResponse,
    OrchestrationTask,
    SubTask,
    TaskStatus,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Keyword → action mapping (simple fallback when LLM is unavailable)
# ---------------------------------------------------------------------------

_KEYWORD_ACTION_MAP: dict[str, str] = {
    "code": "code_generation",
    "generate": "code_generation",
    "build": "code_generation",
    "create": "code_generation",
    "review": "code_review",
    "audit": "code_review",
    "debug": "debugging",
    "fix": "debugging",
    "bug": "debugging",
    "test": "code_generation",
    "deploy": "shell_automation",
    "docker": "shell_automation",
    "search": "web_search",
    "find": "web_search",
    "file": "file_ops",
    "write": "file_ops",
    "read": "file_ops",
}


def _infer_action(description: str) -> str | None:
    """Heuristic: map description keywords to agent action names."""
    lower = description.lower()
    for keyword, action in _KEYWORD_ACTION_MAP.items():
        if keyword in lower:
            return action
    return None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

@dataclass
class RoutingResult:
    """Outcome of a single sub-task routing + execution attempt."""
    sub_task: SubTask
    agent_name: str | None = None
    response: AgentResponse | None = None
    attempts: int = 0
    error: str | None = None
    used_fallback: bool = False


class AgentRouter:
    """Assigns sub-tasks to agents and executes them with retry/fallback.

    Args:
        registry: The adapter registry for agent lookup.
        default_timeout: Per-sub-task timeout in seconds.
        max_retries: Max retry attempts per sub-task.
    """

    def __init__(
        self,
        registry: AdapterRegistry,
        default_timeout: float = 60.0,
        max_retries: int = 3,
    ) -> None:
        self._registry = registry
        self._default_timeout = default_timeout
        self._max_retries = max_retries

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def assign(self, sub_tasks: list[SubTask]) -> list[SubTask]:
        """Assign an agent to each sub-task based on description keywords.

        Populates ``sub_task.assigned_agent`` in place and returns the list.
        """
        for st in sub_tasks:
            st.assigned_agent = await self._pick_agent(st.description)
        return sub_tasks

    async def execute(
        self,
        task: OrchestrationTask,
        context: dict | None = None,
        on_progress: (
            "Callable[[SubTask, RoutingResult], Awaitable[None]] | None"
        ) = None,
    ) -> list[RoutingResult]:
        """Execute all sub-tasks with retry and fallback logic.

        Args:
            task: The orchestration task with sub-tasks and robustness config.
            context: Optional extra context passed to agents.
            on_progress: Optional async callback invoked after EACH sub-task
                completes (success or failure).  Signature:
                ``async (sub_task, routing_result) -> None``.
                Used by the SSE streaming path.

        Returns:
            List of ``RoutingResult``, one per sub-task.
        """
        results: list[RoutingResult] = []
        retries = task.retries if task.retries > 0 else self._max_retries
        fallback = task.fallback_agent

        for st in task.sub_tasks:
            result = await self._execute_one(
                st, retries=retries, fallback_agent=fallback, context=context or {}
            )
            results.append(result)

            # Notify progress callback after each sub-task
            if on_progress:
                await on_progress(st, result)

        return results

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _pick_agent(self, description: str) -> str | None:
        """Pick the best agent for a sub-task description.

        Tries:
          1. Keyword → action → registry.find_by_action
          2. First available agent as fallback
        """
        action = _infer_action(description)
        if action:
            candidates = await self._registry.find_by_action(action)
            if candidates:
                return candidates[0]  # first match

        # Fallback: any registered agent
        agents = await self._registry.list_agents()
        return agents[0] if agents else None

    async def _execute_one(
        self,
        st: SubTask,
        retries: int,
        fallback_agent: str | None,
        context: dict,
    ) -> RoutingResult:
        """Execute a single sub-task with retry on failure."""
        agent_name = st.assigned_agent
        result = RoutingResult(sub_task=st, agent_name=agent_name)

        # ------------------------------------------------------------------
        # Attempt with primary agent
        # ------------------------------------------------------------------
        for attempt in range(1, retries + 1):
            if agent_name is None:
                result.error = "No agent assigned"
                result.attempts = attempt
                st.status = TaskStatus.FAILED
                return result

            try:
                adapter = await self._registry.get(agent_name)
                response = await asyncio.wait_for(
                    adapter.send_message(st.description, context.get("agent_context")),
                    timeout=context.get("timeout", self._default_timeout),
                )
                if response.finish_reason == "stop":
                    st.status = TaskStatus.SUCCESS
                    st.result = response.content
                    result.response = response
                    result.attempts = attempt
                    return result

                # LLM returned non-stop (error/tool_call) — retry
                logger.warning(
                    "Sub-task %s attempt %d/%d: finish_reason=%s",
                    st.id, attempt, retries, response.finish_reason,
                )

            except asyncio.TimeoutError:
                logger.warning(
                    "Sub-task %s attempt %d/%d: timeout", st.id, attempt, retries
                )
            except AgentAdapterError as e:
                logger.warning(
                    "Sub-task %s attempt %d/%d: adapter error: %s",
                    st.id, attempt, retries, e,
                )
            except Exception:
                logger.exception(
                    "Sub-task %s attempt %d/%d: unexpected error", st.id, attempt, retries
                )

            result.attempts = attempt

        # ------------------------------------------------------------------
        # All retries exhausted — try fallback
        # ------------------------------------------------------------------
        if fallback_agent and fallback_agent != agent_name:
            logger.info(
                "Sub-task %s: falling back to '%s'", st.id, fallback_agent
            )
            try:
                adapter = await self._registry.get(fallback_agent)
                response = await asyncio.wait_for(
                    adapter.send_message(st.description, context.get("agent_context")),
                    timeout=context.get("timeout", self._default_timeout),
                )
                if response.finish_reason == "stop":
                    st.status = TaskStatus.SUCCESS
                    st.result = response.content
                    st.assigned_agent = fallback_agent
                    result.response = response
                    result.agent_name = fallback_agent
                    result.used_fallback = True
                    result.attempts = retries + 1
                    return result
            except Exception:
                logger.exception(
                    "Sub-task %s fallback '%s' also failed", st.id, fallback_agent
                )

        # ------------------------------------------------------------------
        # Total failure
        # ------------------------------------------------------------------
        st.status = TaskStatus.FAILED
        result.error = result.error or f"Failed after {retries} retries"
        return result
