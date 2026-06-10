"""API request / response Pydantic models.

These models define the JSON contract for the REST API. They are
separate from ``src.core.schema`` (domain models) — the API layer
validates input here and maps to domain objects for the orchestrator.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

class CreateSessionRequest(BaseModel):
    """Request body for POST /api/sessions."""
    title: str = Field(default="New Chat", min_length=1, max_length=200)
    session_type: str = Field(
        default="single",
        pattern=r"^(single|group)$",
        description="'single' for 1-on-1, 'group' for multi-participant",
    )
    participants: list[str] = Field(
        default_factory=list,
        description="User / agent names in the session (group requires ≥2)",
    )


class CreateSessionResponse(BaseModel):
    """Response body for POST /api/sessions."""
    id: str
    title: str
    type: str
    status: str
    participants: list[str]
    created_at: str
    updated_at: str


class SessionSummary(BaseModel):
    """Summary of a session returned by GET /api/sessions."""
    id: str
    title: str
    type: str
    status: str
    participants: list[str]
    updated_at: str


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

class SendMessageRequest(BaseModel):
    """Request body for POST /api/chat.

    Example::

        {
          "session_id": "abc123",
          "content": "@claude fix the login page and @codex review it",
          "system_prompt": "You are a senior full-stack engineer."
        }
    """
    session_id: str = Field(..., min_length=1, description="Target session ID")
    content: str = Field(..., min_length=1, description="User message text")
    system_prompt: str | None = Field(
        default=None, description="Optional system prompt for agents"
    )


class SubTaskResult(BaseModel):
    """Result of a single sub-task within an orchestration."""
    id: str
    description: str
    assigned_agent: str | None = None
    status: str
    result: str | None = None


class SendMessageResponse(BaseModel):
    """Response body for POST /api/chat.

    Returns immediately with the ``task_id``.  The client can then poll
    or subscribe to ``GET /api/chat/tasks/{task_id}/stream`` for streaming
    progress.
    """
    task_id: str
    session_id: str
    status: str  # "pending" | "running" | "success" | "failed"
    sub_tasks: list[SubTaskResult] = Field(default_factory=list)
    final_result: str | None = None


class TaskStatusResponse(BaseModel):
    """Response body for GET /api/chat/tasks/{task_id}."""
    task_id: str
    session_id: str
    status: str
    sub_tasks: list[SubTaskResult]
    final_result: str | None = None
    created_at: str | None = None
    completed_at: str | None = None


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------

class ErrorResponse(BaseModel):
    """Standard error envelope returned by exception handlers."""
    success: bool = False
    error: str
    detail: Any = None
    request_id: str
