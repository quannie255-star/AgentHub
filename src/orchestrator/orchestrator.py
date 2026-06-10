"""Main Orchestrator — coordinates the full request → response pipeline.

Pipeline::

    User message
        │
        ▼
    TaskParser.parse()          ← extract @-mentions, decompose into sub-tasks
        │
        ▼
    AgentRouter.assign()        ← match sub-tasks to agents by capability
        │
        ▼
    AgentRouter.execute()       ← run each sub-task with retry/fallback/timeout
        │
        ▼
    Result aggregation          ← merge sub-task results into final output
        │
        ▼
    MessageBus.publish()        ← notify subscribers (UI, loggers, etc.)

Async mode (``run_async``):
    Creates the task, fires off background execution, and returns immediately.
    Progress events are published to ``task:<task_id>`` on the MessageBus so
    the SSE endpoint can stream them in real time.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from src.adapters.registry import AdapterRegistry
from src.core.schema import (
    AgentContext,
    ChatMessage,
    MessageRole,
    OrchestrationTask,
    SubTask,
    TaskStatus,
)
from src.core.message_bus import MessageBus
from src.orchestrator.agent_router import AgentRouter, RoutingResult
from src.orchestrator.task_parser import TaskParser, extract_mentions

logger = logging.getLogger(__name__)

# MessageBus topic prefix for task progress streams
TASK_TOPIC_PREFIX = "task:"


def _task_topic(task_id: str) -> str:
    """Return the MessageBus topic for a task's progress stream."""
    return f"{TASK_TOPIC_PREFIX}{task_id}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Progress event helpers
# ---------------------------------------------------------------------------

def _make_progress_event(
    task_id: str, st: SubTask, result: RoutingResult
) -> str:
    """Build a SSE ``data:`` line for a sub-task completion."""
    payload = {
        "event": "progress",
        "task_id": task_id,
        "sub_task_id": st.id,
        "description": st.description,
        "assigned_agent": result.agent_name or st.assigned_agent,
        "status": st.status.value,
        "result": st.result,
        "error": result.error,
    }
    return f"event: progress\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _make_complete_event(task_id: str, task: OrchestrationTask) -> str:
    """Build a SSE ``data:`` line for task completion."""
    payload = {
        "event": "complete",
        "task_id": task_id,
        "status": task.status.value,
        "final_result": task.final_result,
        "sub_task_count": len(task.sub_tasks),
    }
    return f"event: complete\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ======================================================================
# Orchestrator
# ======================================================================

class Orchestrator:
    """Top-level coordinator for the AgentHub pipeline.

    Usage::

        orch = Orchestrator(registry=adapter_registry, message_bus=bus)
        result = await orch.run(
            session_id="s1",
            user_message="@claude build a login page",
        )
    """

    def __init__(
        self,
        registry: AdapterRegistry,
        message_bus: MessageBus | None = None,
        task_parser: TaskParser | None = None,
        agent_router: AgentRouter | None = None,
        default_timeout: float = 60.0,
        default_retries: int = 3,
        fallback_agent: str | None = None,
    ) -> None:
        self._registry = registry
        self._message_bus = message_bus

        self._parser = task_parser or TaskParser()
        self._router = agent_router or AgentRouter(
            registry=registry,
            default_timeout=default_timeout,
            max_retries=default_retries,
        )

        self._default_timeout = default_timeout
        self._default_retries = default_retries
        self._fallback_agent = fallback_agent

    # ------------------------------------------------------------------
    # Main entry point (synchronous — blocks until all sub-tasks complete)
    # ------------------------------------------------------------------

    async def run(
        self,
        session_id: str,
        user_message: str,
        history: list[ChatMessage] | None = None,
        system_prompt: str | None = None,
    ) -> OrchestrationTask:
        """Run the full orchestration pipeline for a user message.

        Args:
            session_id: The chat session this message belongs to.
            user_message: Raw user text (may contain @-mentions).
            history: Prior messages in this session.
            system_prompt: Optional system prompt for agents.

        Returns:
            ``OrchestrationTask`` with status, sub-tasks, and aggregated result.
        """
        # 1. Create task
        task = self._create_task(session_id, user_message)

        # 2. Parse → sub-tasks
        sub_tasks = await self._parser.parse(user_message)
        task.sub_tasks = sub_tasks

        if not sub_tasks:
            task.status = TaskStatus.SUCCESS
            task.final_result = "(no sub-tasks to execute)"
            task.completed_at = _now()
            return task

        # 3. Route → assign agents
        task.sub_tasks = await self._router.assign(task.sub_tasks)

        # 4. Execute with retry/fallback/timeout
        context = self._build_context(session_id, task.task_id, history, system_prompt)
        results = await self._router.execute(task, context=context)

        # 5. Aggregate
        task.final_result = self._aggregate(results)
        task.status = self._derive_status(results)
        task.completed_at = _now()

        # 6. Publish result to message bus
        if self._message_bus:
            await self._publish_result(task)

        return task

    # ------------------------------------------------------------------
    # Async / streaming entry point
    # ------------------------------------------------------------------

    async def run_async(
        self,
        session_id: str,
        user_message: str,
        *,
        task_store: dict[str, OrchestrationTask] | None = None,
        history: list[ChatMessage] | None = None,
        system_prompt: str | None = None,
    ) -> OrchestrationTask:
        """Start orchestration in the background, return immediately.

        The caller receives the task with status ``RUNNING`` and can:
          - Poll ``GET /api/chat/tasks/{task_id}``
          - Stream ``GET /api/chat/tasks/{task_id}/stream`` (SSE via MessageBus)

        Progress events are published to ``task:<task_id>`` on the MessageBus
        as each sub-task completes.  A final ``complete`` event is published
        when the task finishes.

        Args:
            session_id: The chat session this message belongs to.
            user_message: Raw user text (may contain @-mentions).
            task_store: Optional dict for storing task state (used by API
                layer for polling).  If provided, updated on every progress
                event and on completion.
            history: Prior messages in this session.
            system_prompt: Optional system prompt for agents.

        Returns:
            ``OrchestrationTask`` with status ``RUNNING`` and populated sub-tasks.
        """
        # 1. Create task
        task = self._create_task(session_id, user_message, status=TaskStatus.RUNNING)

        # 2. Parse → sub-tasks
        sub_tasks = await self._parser.parse(user_message)
        task.sub_tasks = sub_tasks

        if not sub_tasks:
            task.status = TaskStatus.SUCCESS
            task.final_result = "(no sub-tasks to execute)"
            task.completed_at = _now()
            if task_store is not None:
                task_store[task.task_id] = task
            if self._message_bus:
                await self._message_bus.publish(
                    self._make_system_msg(task, _make_complete_event(task.task_id, task))
                )
            return task

        # 3. Route → assign agents
        task.sub_tasks = await self._router.assign(task.sub_tasks)

        # 4. Store initial task
        if task_store is not None:
            task_store[task.task_id] = task

        # 5. Fire background execution
        asyncio.create_task(
            self._execute_background(
                task=task,
                session_id=session_id,
                history=history,
                system_prompt=system_prompt,
                task_store=task_store,
            )
        )

        return task

    async def _execute_background(
        self,
        task: OrchestrationTask,
        session_id: str,
        history: list[ChatMessage] | None,
        system_prompt: str | None,
        task_store: dict[str, OrchestrationTask] | None,
    ) -> None:
        """Background coroutine that executes sub-tasks with progress events."""
        bus = self._message_bus
        topic = _task_topic(task.task_id)

        async def _on_sub_task_done(st: SubTask, result: RoutingResult) -> None:
            """Publish progress to MessageBus and update task_store."""
            # Publish SSE progress event
            if bus:
                event_text = _make_progress_event(task.task_id, st, result)
                progress_msg = self._make_system_msg(task, event_text)
                await bus.publish(progress_msg)

            # Also publish to the task-specific topic so SSE subscribers see it
            if bus:
                topic_msg = ChatMessage(
                    session_id=topic,
                    role=MessageRole.SYSTEM,
                    sender="orchestrator",
                    content=event_text,
                    metadata={"task_id": task.task_id, "type": "progress"},
                )
                await bus.publish(topic_msg)

            # Update task_store
            if task_store is not None:
                task_store[task.task_id] = task

        try:
            context = self._build_context(session_id, task.task_id, history, system_prompt)
            results = await self._router.execute(
                task, context=context, on_progress=_on_sub_task_done
            )

            task.final_result = self._aggregate(results)
            task.status = self._derive_status(results)
            task.completed_at = _now()

        except Exception:
            logger.exception("Background orchestration failed for task %s", task.task_id)
            task.status = TaskStatus.FAILED
            task.final_result = "Orchestration failed with an unexpected error."
            task.completed_at = _now()

        finally:
            # Publish final complete event
            if bus:
                complete_event = _make_complete_event(task.task_id, task)
                complete_msg = self._make_system_msg(task, complete_event)
                await bus.publish(complete_msg)

                topic_msg = ChatMessage(
                    session_id=topic,
                    role=MessageRole.SYSTEM,
                    sender="orchestrator",
                    content=complete_event,
                    metadata={"task_id": task.task_id, "type": "complete"},
                )
                await bus.publish(topic_msg)

            if task_store is not None:
                task_store[task.task_id] = task

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_task(
        self,
        session_id: str,
        user_message: str,
        status: TaskStatus = TaskStatus.RUNNING,
    ) -> OrchestrationTask:
        """Create an OrchestrationTask with configured defaults."""
        return OrchestrationTask(
            session_id=session_id,
            description=user_message,
            retries=self._default_retries,
            timeout=self._default_timeout,
            fallback_agent=self._fallback_agent,
            status=status,
        )

    def _build_context(
        self,
        session_id: str,
        task_id: str,
        history: list[ChatMessage] | None,
        system_prompt: str | None,
    ) -> dict:
        """Build the execution context dict for AgentRouter."""
        return {
            "timeout": self._default_timeout,
            "agent_context": AgentContext(
                session_id=session_id,
                message_id=task_id,
                history=history or [],
                system_prompt=system_prompt,
            ),
        }

    @staticmethod
    def _make_system_msg(task: OrchestrationTask, content: str) -> ChatMessage:
        """Create a system ChatMessage for the orchestrator's output."""
        return ChatMessage(
            session_id=task.session_id,
            role=MessageRole.SYSTEM,
            sender="orchestrator",
            content=content,
            metadata={
                "task_id": task.task_id,
                "status": task.status.value,
                "sub_task_count": len(task.sub_tasks),
            },
        )

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    @staticmethod
    def _aggregate(results: list[RoutingResult]) -> str:
        """Merge sub-task results into a single output string."""
        parts: list[str] = []
        for i, r in enumerate(results, 1):
            st = r.sub_task
            if st.status == TaskStatus.SUCCESS and st.result:
                label = r.agent_name or "agent"
                parts.append(f"## Sub-task {i}: {st.description}\n"
                             f"**Agent**: {label}\n\n{st.result}")
            else:
                err = r.error or "unknown error"
                parts.append(f"## Sub-task {i}: {st.description}\n"
                             f"**Status**: FAILED — {err}")
        return "\n\n---\n\n".join(parts) if parts else "(empty result)"

    @staticmethod
    def _derive_status(results: list[RoutingResult]) -> TaskStatus:
        """Determine overall task status from sub-task outcomes."""
        if not results:
            return TaskStatus.SUCCESS
        if all(r.sub_task.status == TaskStatus.SUCCESS for r in results):
            return TaskStatus.SUCCESS
        if any(r.sub_task.status == TaskStatus.SUCCESS for r in results):
            return TaskStatus.FAILED  # partial success = overall failure
        return TaskStatus.FAILED

    # ------------------------------------------------------------------
    # Message bus integration
    # ------------------------------------------------------------------

    async def _publish_result(self, task: OrchestrationTask) -> None:
        """Publish the orchestration result as a system message to the bus."""
        assert self._message_bus is not None
        msg = self._make_system_msg(task, task.final_result or "(no output)")
        await self._message_bus.publish(msg)
