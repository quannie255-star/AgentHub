"""API client — thin HTTP wrapper for the AgentHub FastAPI backend.

All functions return Pydantic models or plain dicts.  Errors are raised
as ``AgentHubAPIError`` so the Streamlit UI can catch and display them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterator

import httpx


class AgentHubAPIError(Exception):
    """Raised when the API returns an error or is unreachable."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class AgentHubClient:
    """Synchronous HTTP client for the AgentHub REST API.

    All methods are synchronous because Streamlit's execution model
    re-runs the script top-to-bottom on every interaction — there's no
    long-lived event loop to share.

    Args:
        base_url: FastAPI server root (e.g. ``http://localhost:8000``).
        timeout: Request timeout in seconds.
    """

    def __init__(self, base_url: str = "http://localhost:8000", timeout: float = 30.0) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._client: httpx.Client | None = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """GET /health — returns uptime, version, agent count."""
        return self._get("/health")

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def list_sessions(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """GET /api/sessions — list active sessions."""
        r = self._get(f"/api/sessions/?limit={limit}&offset={offset}")
        return r.get("data", [])

    def create_session(
        self, title: str = "New Chat", session_type: str = "single",
        participants: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /api/sessions — create a new session."""
        body: dict[str, Any] = {"title": title, "session_type": session_type}
        if participants:
            body["participants"] = participants
        r = self._post("/api/sessions/", json=body)
        return r["data"]

    def get_session(self, session_id: str) -> dict[str, Any]:
        """GET /api/sessions/{id} — get a single session."""
        r = self._get(f"/api/sessions/{session_id}")
        return r["data"]

    def archive_session(self, session_id: str) -> dict[str, Any]:
        """PATCH /api/sessions/{id}/archive — archive a session."""
        r = self._patch(f"/api/sessions/{session_id}/archive")
        return r["data"]

    def delete_session(self, session_id: str) -> dict[str, Any]:
        """DELETE /api/sessions/{id} — delete a session."""
        r = self._delete(f"/api/sessions/{session_id}")
        return r.get("data", {})

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------

    def send_message(
        self, session_id: str, content: str, system_prompt: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/chat — send a message (returns task_id immediately)."""
        body: dict[str, Any] = {"session_id": session_id, "content": content}
        if system_prompt:
            body["system_prompt"] = system_prompt
        r = self._post("/api/chat/", json=body)
        return r["data"]

    def get_task(self, task_id: str) -> dict[str, Any]:
        """GET /api/chat/tasks/{task_id} — poll task status."""
        r = self._get(f"/api/chat/tasks/{task_id}")
        return r["data"]

    def stream_task(self, task_id: str) -> Iterator[dict[str, Any]]:
        """GET /api/chat/tasks/{task_id}/stream — stream SSE events.

        Returns an iterator of parsed event dicts.  Each dict has at least
        an ``"event"`` key (``"connected"``, ``"progress"``, ``"complete"``).

        Blocking — call from a background thread when used in Streamlit.

        Usage::

            for event in client.stream_task(task_id):
                if event["event"] == "progress":
                    print(event["assigned_agent"], event["result"])
                elif event["event"] == "complete":
                    break
        """
        url = f"{self._base}/api/chat/tasks/{task_id}/stream"
        try:
            with self.client.stream("GET", url) as response:
                if not response.is_success:
                    raise AgentHubAPIError(
                        f"SSE stream error {response.status_code}",
                        status_code=response.status_code,
                    )
                for line in response.iter_lines():
                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        if not data_str:
                            continue
                        try:
                            yield json.loads(data_str)
                        except json.JSONDecodeError:
                            continue  # skip unparseable lines
        except httpx.ConnectError:
            raise AgentHubAPIError(
                f"Cannot connect to AgentHub API at {self._base}."
            )
        except httpx.TimeoutException:
            raise AgentHubAPIError("SSE stream timed out")

    # ------------------------------------------------------------------
    # Agents (via /health)
    # ------------------------------------------------------------------

    def list_agents(self) -> list[str]:
        """Return agent names currently registered.

        Derived from the health endpoint or the session manager.
        For now we use a static list that matches registered adapters.
        """
        try:
            h = self.health()
            # The health endpoint includes agents_online count but not names.
            # We pull from the OpenAPI schema or fall back to configured list.
            _ = h  # unused for now
        except AgentHubAPIError:
            pass
        # In the future, add GET /api/agents. For now return known agents.
        return ["claude", "codex"]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    def _post(self, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("POST", path, json=json)

    def _patch(self, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("PATCH", path, json=json)

    def _delete(self, path: str) -> dict[str, Any]:
        return self._request("DELETE", path)

    def _request(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base}{path}"
        try:
            r = self.client.request(method, url, json=json)
        except httpx.ConnectError:
            raise AgentHubAPIError(
                f"Cannot connect to AgentHub API at {self._base}. "
                "Is the backend running? (uvicorn src.api.app:create_app --factory)"
            )
        except httpx.TimeoutException:
            raise AgentHubAPIError(f"Request to {path} timed out after {self._timeout}s")

        if not r.is_success:
            detail = ""
            try:
                body = r.json()
                detail = body.get("detail", body.get("error", ""))
            except Exception:
                detail = r.text[:200]
            raise AgentHubAPIError(
                f"API error {r.status_code} on {method} {path}: {detail}",
                status_code=r.status_code,
            )

        return r.json()
