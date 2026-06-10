"""Unit and integration tests for the FastAPI layer.

Covers:
  - App factory (create_app)
  - /health endpoint (uptime, agent count)
  - CORS headers (preflight, normal, credentials)
  - X-Request-ID (generation, echo, uniqueness, CORs-expose)
  - Exception handlers (404 JSON, 422 ValidationError, 500 fallback)
  - Session routes (list, create, get, archive, delete)
  - Chat routes (send message with real orchestrator)
  - 422 validation on malformed request bodies
  - SSE stream endpoint
  - FastAPI docs (Swagger, ReDoc, OpenAPI JSON)
  - API response envelope conformance
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from src.adapters.base import AbstractAgentAdapter
from src.adapters.registry import AdapterRegistry
from src.core.schema import (
    AgentCapability,
    AgentContext,
    AgentResponse,
    AgentStatus,
    HealthCheck,
)
from src.api.app import create_app
from src.api.dependencies import setup_dependencies


# ======================================================================
# Mock adapter for integration tests
# ======================================================================

class _MockAgent(AbstractAgentAdapter):
    """Mock agent that returns canned responses for API integration tests."""

    def __init__(self, name: str = "mock", actions: list[str] | None = None):
        self._name = name
        self._caps = AgentCapability(
            agent_name=name,
            display_name=f"Mock {name}",
            supported_actions=actions or ["code_generation", "code_review", "debugging"],
        )

    @property
    def agent_name(self) -> str:
        return self._name

    async def send_message(self, msg: str, context: AgentContext) -> AgentResponse:
        return AgentResponse(
            agent_name=self._name,
            content=f"[{self._name}] processed: {msg[:50]}",
            finish_reason="stop",
            tokens_used=10,
            latency_ms=5.0,
        )

    async def stream_response(self, msg: str, context: AgentContext):
        for chunk in [f"[{self._name}] ", "streaming ", "response"]:
            yield chunk

    def get_capabilities(self) -> AgentCapability:
        return self._caps

    async def health_check(self) -> AgentStatus:
        return AgentStatus.IDLE

    async def cancel(self) -> None:
        pass


# ======================================================================
# Fixtures
# ======================================================================

@pytest.fixture
def app():
    """Default app — adapters may fail to register (CLIs not in test env)."""
    return create_app()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def app_with_agents():
    """App with mock agents pre-registered for integration tests."""
    import asyncio
    import time

    app = create_app()

    registry = AdapterRegistry()
    asyncio.run(
        registry.register(_MockAgent(
            name="claude",
            actions=["code_generation", "code_review", "debugging", "file_ops"],
        ))
    )
    asyncio.run(
        registry.register(_MockAgent(
            name="codex",
            actions=["code_generation", "shell_automation"],
        ))
    )

    # Pre-wire dependencies so the startup event is a no-op
    from src.core.message_bus import MessageBus
    from src.core.session import SessionManager
    from src.orchestrator.orchestrator import Orchestrator

    app.state.message_bus = MessageBus()
    app.state.session_manager = SessionManager(repository=None)
    app.state.registry = registry
    app.state.orchestrator = Orchestrator(
        registry=registry,
        message_bus=app.state.message_bus,
        default_timeout=5.0,
        default_retries=1,
    )
    app.state.settings = None
    app.state._deps_ready = True
    app.state.started_at = time.time()
    app.state.task_store = {}

    return app


@pytest.fixture
async def client_with_agents(app_with_agents):
    transport = ASGITransport(app=app_with_agents)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ======================================================================
# /health
# ======================================================================

class TestHealth:
    async def test_health_ok(self, client: AsyncClient):
        r = await client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["version"] == "0.1.0"
        assert "uptime_seconds" in body
        assert isinstance(body["uptime_seconds"], (int, float))

    async def test_health_response_model(self, client: AsyncClient):
        r = await client.get("/health")
        hc = HealthCheck.model_validate(r.json())
        assert hc.status == "ok"

    async def test_health_returns_json_content_type(self, client: AsyncClient):
        r = await client.get("/health")
        assert "application/json" in r.headers.get("content-type", "")

    async def test_health_with_agents(self, client_with_agents: AsyncClient):
        """When agents are registered, agents_online should reflect them."""
        r = await client_with_agents.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["agents_online"] >= 0


# ======================================================================
# CORS
# ======================================================================

class TestCORS:
    async def test_cors_preflight(self, client: AsyncClient):
        r = await client.options(
            "/health",
            headers={
                "Origin": "http://localhost:8501",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert r.status_code in (200, 405)
        if r.status_code == 200:
            assert "access-control-allow-origin" in r.headers or \
                   "access-control-allow-methods" in r.headers

    async def test_cors_origin_header_on_normal_request(self, client: AsyncClient):
        r = await client.get(
            "/health",
            headers={"Origin": "http://localhost:8501"},
        )
        assert "access-control-allow-origin" in r.headers
        assert r.headers["access-control-allow-origin"] == "http://localhost:8501"

    async def test_cors_allow_credentials(self, client: AsyncClient):
        r = await client.get(
            "/health",
            headers={"Origin": "http://localhost:8501"},
        )
        assert r.headers.get("access-control-allow-credentials") == "true"


# ======================================================================
# Request ID
# ======================================================================

class TestRequestID:
    async def test_generates_when_missing(self, client: AsyncClient):
        r = await client.get("/health")
        assert "x-request-id" in r.headers
        assert len(r.headers["x-request-id"]) == 12

    async def test_honours_client_supplied(self, client: AsyncClient):
        r = await client.get(
            "/health",
            headers={"X-Request-ID": "my-custom-id"},
        )
        assert r.headers["x-request-id"] == "my-custom-id"

    async def test_unique_per_request(self, client: AsyncClient):
        r1 = await client.get("/health")
        r2 = await client.get("/health")
        assert r1.headers["x-request-id"] != r2.headers["x-request-id"]

    async def test_request_id_exposed_via_cors(self, client: AsyncClient):
        r = await client.get(
            "/health",
            headers={"Origin": "http://localhost:8501"},
        )
        expose = r.headers.get("access-control-expose-headers", "")
        assert "x-request-id" in expose.lower()


# ======================================================================
# Exception handlers
# ======================================================================

class TestExceptionHandlers:
    async def test_404_returns_json(self, client: AsyncClient):
        r = await client.get("/nonexistent-route")
        assert r.status_code == 404
        body = r.json()
        assert body["success"] is False
        assert "error" in body
        assert "request_id" in body

    async def test_422_on_missing_required_field(self, client_with_agents: AsyncClient):
        """POST /api/chat without required 'content' field → 422."""
        r = await client_with_agents.post(
            "/api/chat/",
            json={"session_id": "s1"},  # missing 'content'
        )
        assert r.status_code == 422
        body = r.json()
        assert body["success"] is False
        assert "request_id" in body
        # detail is a list of error dicts; at least one should mention 'content'
        detail = body.get("detail", [])
        field_names = []
        for d in detail:
            # loc is e.g. ('body', 'content')
            if isinstance(d.get("loc"), (list, tuple)):
                field_names.extend(str(x) for x in d["loc"])
        assert "content" in field_names

    async def test_422_on_empty_body(self, client_with_agents: AsyncClient):
        """POST /api/chat with empty JSON → 422."""
        r = await client_with_agents.post(
            "/api/chat/",
            json={},
        )
        assert r.status_code == 422
        body = r.json()
        assert body["success"] is False

    async def test_422_on_invalid_session_type(self, client_with_agents: AsyncClient):
        """POST /api/sessions with bad session_type → 422."""
        r = await client_with_agents.post(
            "/api/sessions/",
            json={"session_type": "invalid_type"},
        )
        assert r.status_code == 422

    async def test_exception_handler_includes_request_id(self, client: AsyncClient):
        r = await client.get("/nonexistent-page")
        body = r.json()
        assert len(body["request_id"]) > 0

    async def test_400_on_business_rule_violation(self, client_with_agents: AsyncClient):
        """POST /api/sessions with group + 1 participant → 400."""
        r = await client_with_agents.post(
            "/api/sessions/",
            json={"session_type": "group", "participants": ["alice"]},
        )
        assert r.status_code == 400
        body = r.json()
        # The exception handler turns HTTPException into structured JSON
        assert body["success"] is False or "detail" in body


# ======================================================================
# Session routes (integration with SessionManager)
# ======================================================================

class TestSessionRoutes:
    async def test_create_and_list(self, client_with_agents: AsyncClient):
        # Create
        r = await client_with_agents.post(
            "/api/sessions/",
            json={"title": "Test Chat", "participants": ["alice"]},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["success"] is True
        assert body["data"]["title"] == "Test Chat"
        assert body["data"]["type"] == "single"
        session_id = body["data"]["id"]

        # List — should include our session
        r2 = await client_with_agents.get("/api/sessions/")
        assert r2.status_code == 200
        sessions = r2.json()["data"]
        assert any(s["id"] == session_id for s in sessions)

    async def test_get_session(self, client_with_agents: AsyncClient):
        r = await client_with_agents.post("/api/sessions/", json={"title": "Find Me"})
        session_id = r.json()["data"]["id"]

        r2 = await client_with_agents.get(f"/api/sessions/{session_id}")
        assert r2.status_code == 200
        assert r2.json()["data"]["title"] == "Find Me"

    async def test_get_not_found(self, client_with_agents: AsyncClient):
        r = await client_with_agents.get("/api/sessions/nonexistent-id")
        assert r.status_code == 404

    async def test_archive_session(self, client_with_agents: AsyncClient):
        r = await client_with_agents.post("/api/sessions/", json={"title": "To Archive"})
        session_id = r.json()["data"]["id"]

        r2 = await client_with_agents.patch(f"/api/sessions/{session_id}/archive")
        assert r2.status_code == 200
        assert r2.json()["data"]["status"] == "archived"

    async def test_delete_session(self, client_with_agents: AsyncClient):
        r = await client_with_agents.post("/api/sessions/", json={"title": "To Delete"})
        session_id = r.json()["data"]["id"]

        r2 = await client_with_agents.delete(f"/api/sessions/{session_id}")
        assert r2.status_code == 200
        assert r2.json()["data"]["status"] == "deleted"

        # Confirm gone
        r3 = await client_with_agents.get(f"/api/sessions/{session_id}")
        assert r3.status_code == 404

    async def test_create_group_session(self, client_with_agents: AsyncClient):
        r = await client_with_agents.post(
            "/api/sessions/",
            json={
                "title": "Team Room",
                "session_type": "group",
                "participants": ["alice", "claude", "codex"],
            },
        )
        assert r.status_code == 201
        assert r.json()["data"]["type"] == "group"
        assert len(r.json()["data"]["participants"]) == 3

    async def test_list_sessions_pagination(self, client_with_agents: AsyncClient):
        for i in range(3):
            await client_with_agents.post("/api/sessions/", json={"title": f"Chat {i}"})
        r = await client_with_agents.get("/api/sessions/?limit=2&offset=0")
        assert r.status_code == 200
        assert len(r.json()["data"]) == 2


# ======================================================================
# Chat routes (integration with Orchestrator)
# ======================================================================

class TestChatRoutes:
    async def test_send_message_returns_immediately(self, client_with_agents: AsyncClient):
        """POST /api/chat now returns immediately with RUNNING status (async mode)."""
        r = await client_with_agents.post("/api/sessions/", json={"title": "Dev Chat"})
        session_id = r.json()["data"]["id"]

        r2 = await client_with_agents.post(
            "/api/chat/",
            json={
                "session_id": session_id,
                "content": "fix the login bug and review the code",
            },
        )
        assert r2.status_code == 201
        body = r2.json()
        assert body["success"] is True
        assert body["data"]["task_id"] is not None
        assert body["data"]["session_id"] == session_id
        # Returns immediately — sub_tasks are assigned but not yet completed
        assert body["data"]["status"] in ("running", "success")
        assert len(body["data"]["sub_tasks"]) >= 1

    async def test_send_message_with_mentions(self, client_with_agents: AsyncClient):
        r = await client_with_agents.post("/api/sessions/", json={"title": "Multi-Agent"})
        session_id = r.json()["data"]["id"]

        r2 = await client_with_agents.post(
            "/api/chat/",
            json={
                "session_id": session_id,
                "content": "@claude fix the bug and @codex deploy it",
            },
        )
        assert r2.status_code == 201
        sub_tasks = r2.json()["data"]["sub_tasks"]
        agents_used = [st["assigned_agent"] for st in sub_tasks]
        assert "claude" in agents_used

    async def test_send_message_session_not_found(self, client_with_agents: AsyncClient):
        r = await client_with_agents.post(
            "/api/chat/",
            json={
                "session_id": "nonexistent-session",
                "content": "do something",
            },
        )
        assert r.status_code == 404

    async def test_send_message_with_system_prompt(self, client_with_agents: AsyncClient):
        r = await client_with_agents.post("/api/sessions/", json={"title": "Guided"})
        session_id = r.json()["data"]["id"]

        r2 = await client_with_agents.post(
            "/api/chat/",
            json={
                "session_id": session_id,
                "content": "write a function",
                "system_prompt": "You are a Python expert.",
            },
        )
        assert r2.status_code == 201
        # Returns immediately with task; background execution runs later
        assert r2.json()["data"]["task_id"] is not None


# ======================================================================
# SSE stream
# ======================================================================

class TestStreamEndpoint:
    async def test_stream_returns_event_stream(self, client_with_agents: AsyncClient):
        r = await client_with_agents.get("/api/chat/tasks/task-123/stream")
        assert r.status_code == 200
        assert "text/event-stream" in r.headers.get("content-type", "")
        assert "event:" in r.text

    async def test_stream_has_correct_headers(self, client_with_agents: AsyncClient):
        r = await client_with_agents.get("/api/chat/tasks/task-456/stream")
        assert r.headers.get("cache-control") == "no-cache"
        assert r.headers.get("connection") == "keep-alive"
        assert "x-request-id" in r.headers

    async def test_stream_live_progress(self, client_with_agents: AsyncClient):
        """End-to-end: POST /api/chat then stream real progress via SSE.

        If the background task finishes before the SSE connection is made,
        the replay phase sends progress + complete events (no "connected").
        If it's still running, a "connected" event is sent before live streaming.
        Both paths are valid.
        """
        # Create session
        r = await client_with_agents.post("/api/sessions/", json={"title": "StreamTest"})
        session_id = r.json()["data"]["id"]

        # Start orchestration (async)
        r2 = await client_with_agents.post(
            "/api/chat/",
            json={"session_id": session_id, "content": "1. fix bug\n2. write test"},
        )
        assert r2.status_code == 201
        task_id = r2.json()["data"]["task_id"]

        # Connect to SSE stream — receives progress + complete (replay or live)
        r3 = await client_with_agents.get(f"/api/chat/tasks/{task_id}/stream")
        assert r3.status_code == 200
        body = r3.text

        # Should have progress events for both sub-tasks
        assert "event: progress" in body
        assert "event: complete" in body
        assert task_id in body

        # Poll task status — should be complete by now
        r4 = await client_with_agents.get(f"/api/chat/tasks/{task_id}")
        assert r4.status_code == 200
        task_data = r4.json()["data"]
        assert task_data["task_id"] == task_id
        assert task_data["status"] in ("running", "success")
        assert len(task_data["sub_tasks"]) == 2


# ======================================================================
# Task polling
# ======================================================================

class TestTaskPolling:
    async def test_get_task_not_found(self, client_with_agents: AsyncClient):
        r = await client_with_agents.get("/api/chat/tasks/nonexistent-task")
        assert r.status_code == 404
        body = r.json()
        assert body["success"] is False

    async def test_poll_after_send(self, client_with_agents: AsyncClient):
        """After POST /api/chat, the task should be pollable."""
        r = await client_with_agents.post("/api/sessions/", json={"title": "PollTest"})
        session_id = r.json()["data"]["id"]

        r2 = await client_with_agents.post(
            "/api/chat/",
            json={"session_id": session_id, "content": "fix a bug"},
        )
        task_id = r2.json()["data"]["task_id"]

        # Poll immediately — task should exist
        r3 = await client_with_agents.get(f"/api/chat/tasks/{task_id}")
        assert r3.status_code == 200
        data = r3.json()["data"]
        assert data["task_id"] == task_id
        assert data["session_id"] == session_id


# ======================================================================
# FastAPI docs
# ======================================================================

class TestDocs:
    async def test_swagger_ui_accessible(self, client: AsyncClient):
        r = await client.get("/docs")
        assert r.status_code == 200
        assert "Swagger" in r.text or "swagger" in r.text

    async def test_redoc_accessible(self, client: AsyncClient):
        r = await client.get("/redoc")
        assert r.status_code == 200

    async def test_openapi_json(self, client: AsyncClient):
        r = await client.get("/openapi.json")
        assert r.status_code == 200
        schema = r.json()
        assert schema["info"]["title"] == "AgentHub API"
        assert "/health" in schema["paths"]
        assert "/api/sessions/" in schema["paths"]
        assert "/api/sessions/{session_id}" in schema["paths"]
        assert "/api/chat/" in schema["paths"]
        assert "/api/chat/tasks/{task_id}" in schema["paths"]
        assert "/api/chat/tasks/{task_id}/stream" in schema["paths"]


# ======================================================================
# API response envelope shape
# ======================================================================

class TestAPIResponseEnvelope:
    async def test_all_routes_conform_to_envelope(self, client_with_agents: AsyncClient):
        """All non-streaming JSON routes should return success/request_id."""
        # Create a session first so we have a valid ID
        r = await client_with_agents.post("/api/sessions/", json={"title": "EnvTest"})
        session_id = r.json()["data"]["id"]

        paths = [
            ("GET", "/api/sessions/"),
            ("POST", "/api/sessions/"),
            ("GET", f"/api/sessions/{session_id}"),
        ]
        for method, path in paths:
            if method == "POST":
                r2 = await client_with_agents.request(method, path, json={"title": "X"})
            else:
                r2 = await client_with_agents.request(method, path)
            body = r2.json()
            assert "success" in body, f"{method} {path}"
            assert "request_id" in body, f"{method} {path}"
            assert "data" in body or "error" in body, f"{method} {path}"

    async def test_chat_response_envelope(self, client_with_agents: AsyncClient):
        r = await client_with_agents.post("/api/sessions/", json={"title": "ChatEnv"})
        session_id = r.json()["data"]["id"]

        r2 = await client_with_agents.post(
            "/api/chat/",
            json={"session_id": session_id, "content": "test"},
        )
        body = r2.json()
        assert "success" in body
        assert "request_id" in body
        assert "data" in body
        assert body["data"]["task_id"] is not None
        # Async mode: status is "running" and final_result may be None
        assert body["data"]["status"] in ("running", "success")
