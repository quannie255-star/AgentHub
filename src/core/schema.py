"""AgentHub shared data models (Pydantic V2).

All agents, the orchestrator, the API layer, and the UI share these models.
Schema MUST be written first — every downstream module depends on it.

Design rules (from CLAUDE.md bug library):
  - Pydantic V2 syntax only (no ``class Config``)
  - URL fields use ``str``, never ``HttpUrl`` (serialisation hazard)
  - Enum classes use ``(str, Enum)`` for JSON compatibility
  - Core fields that need traceability use ``AnnotatedFinding`` + ``Evidence``
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator


# ======================================================================
# Helpers
# ======================================================================

def _new_id() -> str:
    """Generate a short unique identifier."""
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ======================================================================
# Evidence & Traceability (from playbook pattern)
# ======================================================================

class Evidence(BaseModel):
    """A piece of evidence backing a claim."""
    source: str = Field(..., description="Source identifier (URL, agent name, etc.)")
    excerpt: str = Field("", description="Relevant excerpt or quote")
    timestamp: datetime = Field(default_factory=_now)


class AnnotatedFinding(BaseModel):
    """A finding (strength / weakness / insight) with optional evidence."""
    text: str
    evidence: list[Evidence] = Field(default_factory=list)


# ======================================================================
# Chat & Session
# ======================================================================

class MessageRole(str, Enum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class SessionType(str, Enum):
    SINGLE = "single"    # 1-on-1 chat
    GROUP = "group"      # group chat with @-mentions


class SessionStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class ChatMessage(BaseModel):
    """A single message in a chat session."""
    id: str = Field(default_factory=_new_id)
    session_id: str
    role: MessageRole
    sender: str = Field(..., description="User name or agent name")
    content: str
    mentioned_agents: list[str] = Field(
        default_factory=list,
        description="Agent names extracted from @-mentions (group chat routing)",
    )
    parent_message_id: str | None = Field(
        default=None, description="For threaded replies"
    )
    created_at: datetime = Field(default_factory=_now)

    # Metadata for UI rendering
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatSession(BaseModel):
    """A chat session — can be single or group."""
    id: str = Field(default_factory=_new_id)
    title: str = "New Chat"
    type: SessionType = SessionType.SINGLE
    status: SessionStatus = SessionStatus.ACTIVE
    participants: list[str] = Field(
        default_factory=list,
        description="User and agent names participating in this session",
    )
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ======================================================================
# Agent Adapter
# ======================================================================

class AgentCapability(BaseModel):
    """Describes what an agent can do — used by the router for task assignment."""
    agent_name: str
    display_name: str = ""
    description: str = ""
    supported_actions: list[str] = Field(
        default_factory=list,
        examples=["code_generation", "code_review", "web_search", "file_ops"],
    )
    max_context_tokens: int = 200_000
    supports_streaming: bool = True
    supports_images: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentContext(BaseModel):
    """Context object passed to an agent for each request."""
    session_id: str
    message_id: str
    history: list[ChatMessage] = Field(default_factory=list)
    system_prompt: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class AgentResponse(BaseModel):
    """Response from an agent after processing a message."""
    message_id: str = Field(default_factory=_new_id)
    agent_name: str
    content: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    finish_reason: Literal["stop", "tool_call", "error", "timeout"] = "stop"
    tokens_used: int = 0
    latency_ms: float = 0.0
    created_at: datetime = Field(default_factory=_now)


# ======================================================================
# Orchestration
# ======================================================================

class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class SubTask(BaseModel):
    """A single sub-task produced by the TaskParser."""
    id: str = Field(default_factory=_new_id)
    description: str
    assigned_agent: str | None = None  # filled by AgentRouter
    dependencies: list[str] = Field(default_factory=list)  # sub-task IDs that must finish first
    status: TaskStatus = TaskStatus.PENDING
    result: str | None = None


class OrchestrationTask(BaseModel):
    """Top-level orchestration task — the unit of work for the Orchestrator.

    Includes robustness fields for production use:
      - ``retries``: max retry count on failure
      - ``timeout``: max wall-clock seconds before abort
      - ``fallback_agent``: agent to use if the primary agent exhausts retries
    """

    task_id: str = Field(default_factory=_new_id)
    session_id: str
    description: str = Field(..., description="User's original request text")

    # --- Robustness fields ---
    retries: int = 3               # max retries per sub-task
    timeout: float = 60.0          # seconds, per sub-task
    fallback_agent: str | None = None  # fallback if primary agent fails

    # --- Decomposition result ---
    sub_tasks: list[SubTask] = Field(default_factory=list)

    # --- Status ---
    status: TaskStatus = TaskStatus.PENDING
    created_at: datetime = Field(default_factory=_now)
    completed_at: datetime | None = None

    # --- Aggregated result ---
    final_result: str | None = None

    @model_validator(mode="after")
    def _apply_orchestrator_defaults(self) -> "OrchestrationTask":
        """Apply defaults from config if not explicitly set.

        In the orchestrator flow these will be overridden from AgentHubSettings,
        but the schema itself carries sensible defaults for standalone use.
        """
        return self


class AgentStatus(str, Enum):
    IDLE = "idle"
    BUSY = "busy"
    ERROR = "error"
    OFFLINE = "offline"


class AgentHeartbeat(BaseModel):
    """Periodic heartbeat from an agent."""
    agent_name: str
    status: AgentStatus = AgentStatus.IDLE
    current_task_id: str | None = None
    last_seen: datetime = Field(default_factory=_now)


# ======================================================================
# Tool Outputs
# ======================================================================

class DiffResult(BaseModel):
    """Result from the code-diff tool."""
    file_path: str
    original: str
    modified: str
    unified_diff: str
    language: str = ""


class PreviewResult(BaseModel):
    """Result from the web-preview tool."""
    url: str
    port: int
    status: Literal["running", "error", "stopped"] = "running"


class DeployResult(BaseModel):
    """Result from the one-click-deploy tool."""
    service_name: str
    status: Literal["deployed", "failed", "building"]
    log: str = ""
    url: str | None = None


# ======================================================================
# API Envelope
# ======================================================================

class APIResponse(BaseModel):
    """Standard API response envelope."""
    success: bool = True
    data: Any = None
    error: str | None = None
    request_id: str = Field(default_factory=_new_id)
    timestamp: datetime = Field(default_factory=_now)


class HealthCheck(BaseModel):
    """Response from /health endpoint."""
    status: Literal["ok", "degraded", "down"] = "ok"
    version: str = "0.1.0"
    uptime_seconds: float = 0.0
    agents_online: int = 0
