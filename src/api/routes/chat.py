"""Chat routes — send messages to the Orchestrator, stream results.

Wired to ``Orchestrator`` via ``request.app.state.orchestrator``.

Endpoint summary:
  - ``POST /api/chat``         — start orchestration, return task_id immediately.
  - ``GET  /api/chat/tasks/{task_id}``       — poll task status.
  - ``GET  /api/chat/tasks/{task_id}/stream`` — SSE streaming of task progress.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from src.api.routes.schemas import (
    SendMessageRequest,
    SendMessageResponse,
    SubTaskResult,
    TaskStatusResponse,
)
from src.core.schema import APIResponse, ChatMessage, MessageRole, TaskStatus
from src.orchestrator.orchestrator import TASK_TOPIC_PREFIX, _task_topic

logger = logging.getLogger("agenthub.api")

router = APIRouter(prefix="/api/chat", tags=["Chat"])


def _sub_task_results(task) -> list[SubTaskResult]:
    """Convert an OrchestrationTask's sub_tasks to API response models."""
    return [
        SubTaskResult(
            id=st.id,
            description=st.description,
            assigned_agent=st.assigned_agent,
            status=st.status.value,
            result=st.result,
        )
        for st in (task.sub_tasks or [])
    ]


# ---------------------------------------------------------------------------
# POST /api/chat  — start orchestration (async, non-blocking)
# ---------------------------------------------------------------------------


@router.post("/", status_code=201, response_model=APIResponse)
async def send_message(body: SendMessageRequest, request: Request) -> APIResponse:
    """Send a user message to the orchestrator (async mode).

    The message is parsed, decomposed, and routed to agents.  Execution
    runs in the background — the response returns immediately with a
    ``task_id``.  Use the SSE stream or polling endpoint to follow progress.

    Returns ``201`` with:
      - ``task_id``: unique ID for tracking progress
      - ``status``: "running"
      - ``sub_tasks``: parsed / assigned sub-tasks (results still pending)
    """
    orch = request.app.state.orchestrator
    mgr = request.app.state.session_manager
    task_store: dict = request.app.state.task_store

    # Verify session exists
    try:
        await mgr.get(body.session_id)
    except Exception:
        raise HTTPException(
            status_code=404,
            detail=f"Session '{body.session_id}' not found",
        )

    # Run async — returns immediately with RUNNING status
    task = await orch.run_async(
        session_id=body.session_id,
        user_message=body.content,
        task_store=task_store,
        system_prompt=body.system_prompt,
    )

    sub_results = _sub_task_results(task)

    return APIResponse(
        success=True,
        data=SendMessageResponse(
            task_id=task.task_id,
            session_id=body.session_id,
            status=task.status.value,
            sub_tasks=sub_results,
            final_result=task.final_result,
        ).model_dump(),
        request_id=getattr(request.state, "request_id", None),
    )


# ---------------------------------------------------------------------------
# GET /api/chat/tasks/{task_id}  — poll status
# ---------------------------------------------------------------------------


@router.get("/tasks/{task_id}", response_model=APIResponse)
async def get_task_status(task_id: str, request: Request) -> APIResponse:
    """Poll the status of a previously submitted task.

    Returns the current ``OrchestrationTask`` state including any
    completed sub-task results.
    """
    task_store: dict = request.app.state.task_store
    task = task_store.get(task_id)

    if task is None:
        raise HTTPException(
            status_code=404,
            detail=f"Task '{task_id}' not found. It may have expired or never existed.",
        )

    return APIResponse(
        success=task.status != TaskStatus.FAILED,
        data=TaskStatusResponse(
            task_id=task.task_id,
            session_id=task.session_id,
            status=task.status.value,
            sub_tasks=_sub_task_results(task),
            final_result=task.final_result,
            created_at=task.created_at.isoformat() if task.created_at else None,
            completed_at=task.completed_at.isoformat() if task.completed_at else None,
        ).model_dump(),
        error=None if task.status != TaskStatus.FAILED else "One or more sub-tasks failed",
        request_id=getattr(request.state, "request_id", None),
    )


# ---------------------------------------------------------------------------
# GET /api/chat/tasks/{task_id}/stream  — SSE stream
# ---------------------------------------------------------------------------


@router.get("/tasks/{task_id}/stream")
async def stream_task(task_id: str, request: Request):
    """Stream task progress via Server-Sent Events.

    **Phase 1 — Replay:** First, any sub-tasks that have *already* completed
    are sent as ``event: progress`` catch-up events.

    **Phase 2 — Live:** Then the endpoint subscribes to the MessageBus topic
    ``task:<task_id>`` and streams events as they arrive.  The stream closes
    after receiving ``event: complete`` or when the client disconnects.

    Event types:
      - ``event: connected`` — stream established
      - ``event: progress`` — a sub-task completed (success or failure)
      - ``event: complete`` — the entire task finished

    Returns ``text/event-stream``.
    """
    task_store: dict = request.app.state.task_store
    bus = request.app.state.message_bus
    rid = getattr(request.state, "request_id", "-")
    topic = _task_topic(task_id)

    async def _event_stream():
        # --- Phase 1: Replay already-completed sub-tasks ---
        task = task_store.get(task_id)

        if task is None:
            # Task never existed — send complete-not-found and close
            payload = {
                "event": "complete",
                "task_id": task_id,
                "status": "not_found",
                "message": "Task not found",
            }
            yield f"event: complete\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            return

        if task.sub_tasks:
            for st in task.sub_tasks:
                if st.status in (TaskStatus.SUCCESS, TaskStatus.FAILED):
                    payload = {
                        "event": "progress",
                        "task_id": task_id,
                        "sub_task_id": st.id,
                        "description": st.description,
                        "assigned_agent": st.assigned_agent,
                        "status": st.status.value,
                        "result": st.result,
                    }
                    yield f"event: progress\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        # If task already completed, send complete and stop
        if task.status in (TaskStatus.SUCCESS, TaskStatus.FAILED):
            payload = {
                "event": "complete",
                "task_id": task_id,
                "status": task.status.value,
                "final_result": task.final_result,
            }
            yield f"event: complete\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            return

        # --- Phase 2: Live subscription ---
        yield f"event: connected\ndata: {{\"task_id\": \"{task_id}\", \"request_id\": \"{rid}\"}}\n\n"

        sub = await bus.subscribe(topic)
        try:
            async for msg in sub:
                yield msg.content
                if '"event": "complete"' in msg.content:
                    break
        except asyncio.CancelledError:
            pass  # client disconnected
        finally:
            await bus.unsubscribe(sub)

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "X-Request-ID": rid,
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
