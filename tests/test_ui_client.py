"""Unit and integration tests for the UI API client.

Covers:
  - AgentHubClient construction
  - Health check
  - Session CRUD (list, create, get, archive, delete)
  - Chat (send message, poll task)
  - Error handling (connection refused, HTTP errors)
  - Integration against a running FastAPI test app
"""

from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest
import uvicorn

from src.ui.api_client import AgentHubAPIError, AgentHubClient


# ======================================================================
# Helpers — run test FastAPI app in a background uvicorn server
# ======================================================================

def _build_test_app():
    """Create a FastAPI app wired with mock agents (shared with test_api.py)."""
    from src.api.app import create_app
    from src.adapters.registry import AdapterRegistry
    from src.core.message_bus import MessageBus
    from src.core.session import SessionManager
    from src.orchestrator.orchestrator import Orchestrator

    # Use MockAgent from test_api
    from tests.test_api import _MockAgent

    app = create_app()

    registry = AdapterRegistry()
    asyncio.run(registry.register(
        _MockAgent(name="claude", actions=["code_generation", "code_review", "debugging"])
    ))
    asyncio.run(registry.register(
        _MockAgent(name="codex", actions=["code_generation", "shell_automation"])
    ))

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


@pytest.fixture(scope="session")
def live_server():
    """Start the test FastAPI app in a background uvicorn server.

    Returns the base URL as a string (e.g. ``http://127.0.0.1:PORT``).
    """
    import socket

    # Find a free port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    app = _build_test_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for the server to start
    for _ in range(50):  # max 5 seconds
        if server.started:
            break
        time.sleep(0.1)

    base_url = f"http://127.0.0.1:{port}"
    yield base_url

    server.should_exit = True
    thread.join(timeout=2.0)


@pytest.fixture
def client(live_server: str) -> AgentHubClient:
    """Return an AgentHubClient pointed at the live test server."""
    return AgentHubClient(base_url=live_server, timeout=10.0)


# ======================================================================
# Health
# ======================================================================

class TestHealth:
    def test_health(self, client: AgentHubClient):
        h = client.health()
        assert h["status"] == "ok"
        assert h["version"] == "0.1.0"

    def test_connection_refused(self):
        c = AgentHubClient(base_url="http://127.0.0.1:19999", timeout=0.5)
        with pytest.raises(AgentHubAPIError):
            c.health()


# ======================================================================
# Sessions
# ======================================================================

class TestSessions:
    def test_create_and_list(self, client: AgentHubClient):
        s = client.create_session(title="UI Test")
        assert s["title"] == "UI Test"
        assert s["id"] is not None

        sessions = client.list_sessions()
        ids = [x["id"] for x in sessions]
        assert s["id"] in ids

    def test_get_session(self, client: AgentHubClient):
        s = client.create_session(title="Get Me")
        fetched = client.get_session(s["id"])
        assert fetched["title"] == "Get Me"

    def test_get_not_found(self, client: AgentHubClient):
        with pytest.raises(AgentHubAPIError) as exc:
            client.get_session("nonexistent")
        assert exc.value.status_code == 404

    def test_archive(self, client: AgentHubClient):
        s = client.create_session(title="Archive Me")
        archived = client.archive_session(s["id"])
        assert archived["status"] == "archived"

    def test_delete(self, client: AgentHubClient):
        s = client.create_session(title="Delete Me")
        client.delete_session(s["id"])
        with pytest.raises(AgentHubAPIError):
            client.get_session(s["id"])

    def test_create_group(self, client: AgentHubClient):
        s = client.create_session(
            title="Team",
            session_type="group",
            participants=["alice", "claude", "codex"],
        )
        assert s["type"] == "group"
        assert len(s["participants"]) == 3


# ======================================================================
# Chat
# ======================================================================

class TestChat:
    def test_send_message(self, client: AgentHubClient):
        s = client.create_session(title="Chat Test")
        result = client.send_message(session_id=s["id"], content="fix the bug")
        assert result["task_id"] is not None
        assert result["status"] in ("running", "success")

    def test_poll_task(self, client: AgentHubClient):
        s = client.create_session(title="Poll Test")
        result = client.send_message(session_id=s["id"], content="write tests")
        task = client.get_task(result["task_id"])
        assert task["task_id"] == result["task_id"]

    def test_send_to_bad_session(self, client: AgentHubClient):
        with pytest.raises(AgentHubAPIError) as exc:
            client.send_message(session_id="no-such-session", content="hi")
        assert exc.value.status_code == 404

    def test_with_system_prompt(self, client: AgentHubClient):
        s = client.create_session(title="Prompt Test")
        result = client.send_message(
            session_id=s["id"],
            content="write code",
            system_prompt="You are an expert.",
        )
        assert result["task_id"] is not None


# ======================================================================
# Error handling
# ======================================================================

class TestErrors:
    def test_http_error_includes_status(self, client: AgentHubClient):
        with pytest.raises(AgentHubAPIError) as exc:
            client.get_session("nonexistent-id")
        assert exc.value.status_code == 404

    def test_timeout(self):
        # Short timeout against non-existent port → connection refused or timeout
        c = AgentHubClient(base_url="http://127.0.0.1:19999", timeout=0.1)
        with pytest.raises(AgentHubAPIError):
            c.health()


# ======================================================================
# Agent listing
# ======================================================================

class TestAgents:
    def test_list_agents(self, client: AgentHubClient):
        agents = client.list_agents()
        assert isinstance(agents, list)
        assert "claude" in agents
        assert "codex" in agents


# ======================================================================
# Client lifecycle
# ======================================================================

class TestLifecycle:
    def test_close_and_reopen(self, client: AgentHubClient):
        client.close()
        # After close, the client should create a new transport on next use
        assert client._client is None
        h = client.health()  # should auto-reconnect
        assert h["status"] == "ok"
