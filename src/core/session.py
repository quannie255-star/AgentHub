"""Session manager — lifecycle for single/group/multi-session chats.

Responsibilities:
  - Create, retrieve, list, archive, and delete ChatSession objects.
  - Enforce invariants (e.g. group sessions must have ≥2 participants).
  - Provide an in-memory store with repository-backend persistence hooks.

The session manager *owns* session state; the repository is an optional
durability backend. When no repository is provided the manager is
in-memory only (useful for tests and lightweight deploys).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Callable

from src.core.schema import ChatSession, SessionStatus, SessionType
from src.repository.base import SessionRepository


class SessionError(Exception):
    """Raised when a session invariant is violated."""


class SessionNotFoundError(SessionError):
    """Raised when a session ID is not found."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SessionManager:
    """Manages the lifecycle of all chat sessions.

    Thread-safe for async usage (all mutation is behind an asyncio.Lock).

    Usage::

        mgr = SessionManager(repo=sqlite_repo)
        session = mgr.create(title="Bug Hunt", type=SessionType.GROUP,
                             participants=["alice", "claude", "codex"])
        active = mgr.list_active()
        mgr.archive(session.id)
    """

    def __init__(self, repository: SessionRepository | None = None) -> None:
        self._repo = repository
        self._sessions: dict[str, ChatSession] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create(
        self,
        title: str = "New Chat",
        session_type: SessionType = SessionType.SINGLE,
        participants: list[str] | None = None,
        metadata: dict | None = None,
    ) -> ChatSession:
        """Create a new session and persist it.

        Args:
            title: Human-readable session name.
            session_type: ``SINGLE`` (1-on-1) or ``GROUP`` (multi-participant with @-mentions).
            participants: List of user/agent names. Group sessions must have ≥2.
            metadata: Optional key-value metadata bag.
        """
        participants = participants or []

        # --- invariants ---
        if session_type == SessionType.GROUP and len(participants) < 2:
            raise SessionError("Group sessions require at least 2 participants")

        session = ChatSession(
            title=title,
            type=session_type,
            participants=participants,
            metadata=metadata or {},
        )

        async with self._lock:
            self._sessions[session.id] = session

        if self._repo:
            await self._repo.save(session)

        return session

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get(self, session_id: str) -> ChatSession:
        """Return a session by ID, or raise ``SessionNotFoundError``."""
        # Check in-memory first
        async with self._lock:
            if session_id in self._sessions:
                return self._sessions[session_id]

        # Fall back to repository
        if self._repo:
            session = await self._repo.get_by_id(session_id)
            if session:
                async with self._lock:
                    self._sessions[session.id] = session
                return session

        raise SessionNotFoundError(f"Session '{session_id}' not found")

    async def list_active(
        self, limit: int = 20, offset: int = 0
    ) -> list[ChatSession]:
        """Return active sessions, most-recently-updated first."""
        # Merge in-memory + repo, deduplicate by ID
        seen: dict[str, ChatSession] = {}

        if self._repo:
            for s in await self._repo.list_active(limit=200, offset=0):
                seen[s.id] = s

        async with self._lock:
            for s in self._sessions.values():
                if s.status == SessionStatus.ACTIVE:
                    seen[s.id] = s

        active = [s for s in seen.values() if s.status == SessionStatus.ACTIVE]
        active.sort(key=lambda s: s.updated_at, reverse=True)
        return active[offset : offset + limit]

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def update_title(self, session_id: str, title: str) -> ChatSession:
        session = await self.get(session_id)
        session.title = title
        session.updated_at = _now()
        async with self._lock:
            self._sessions[session_id] = session
        if self._repo:
            await self._repo.save(session)
        return session

    async def add_participant(self, session_id: str, name: str) -> ChatSession:
        session = await self.get(session_id)
        if name not in session.participants:
            session.participants.append(name)
            session.updated_at = _now()
        async with self._lock:
            self._sessions[session_id] = session
        if self._repo:
            await self._repo.save(session)
        return session

    async def remove_participant(self, session_id: str, name: str) -> ChatSession:
        session = await self.get(session_id)
        if name in session.participants:
            session.participants.remove(name)
            session.updated_at = _now()
        # Group invariant: must have ≥2 after removal (or convert to single?)
        if session.type == SessionType.GROUP and len(session.participants) < 2:
            raise SessionError("Group sessions must retain at least 2 participants")
        async with self._lock:
            self._sessions[session_id] = session
        if self._repo:
            await self._repo.save(session)
        return session

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def archive(self, session_id: str) -> ChatSession:
        return await self._set_status(session_id, SessionStatus.ARCHIVED)

    async def delete(self, session_id: str) -> None:
        session = await self.get(session_id)
        session.status = SessionStatus.DELETED
        session.updated_at = _now()
        async with self._lock:
            self._sessions.pop(session_id, None)
        if self._repo:
            await self._repo.delete(session_id)

    async def _set_status(self, session_id: str, status: SessionStatus) -> ChatSession:
        session = await self.get(session_id)
        session.status = status
        session.updated_at = _now()
        async with self._lock:
            self._sessions[session_id] = session
        if self._repo:
            await self._repo.update_status(session_id, status)
        return session
