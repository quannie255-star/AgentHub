"""Unit tests for SessionManager (src/core/session.py).

Covers:
  - Create single / group sessions
  - Invariant enforcement (group must have ≥2 participants)
  - Read (get, list_active)
  - Update (title, participants)
  - Lifecycle (archive, delete)
  - In-memory mode (no repository)
  - Repository-backed mode (integration with MemorySessionRepository)
"""

from __future__ import annotations

import pytest

from src.core.schema import ChatSession, SessionStatus, SessionType
from src.core.session import SessionManager, SessionError, SessionNotFoundError
from src.repository.base import SessionRepository


# ======================================================================
# In-memory stub repository (for testing without SQLite)
# ======================================================================

class MemorySessionRepository(SessionRepository):
    """In-memory session repo for unit tests."""

    def __init__(self) -> None:
        self.sessions: dict[str, ChatSession] = {}

    async def save(self, session: ChatSession) -> None:
        self.sessions[session.id] = session.model_copy(deep=True)

    async def get_by_id(self, session_id: str) -> ChatSession | None:
        return self.sessions.get(session_id)

    async def list_active(
        self, limit: int = 20, offset: int = 0
    ) -> list[ChatSession]:
        active = [
            s for s in self.sessions.values() if s.status == SessionStatus.ACTIVE
        ]
        active.sort(key=lambda s: s.updated_at, reverse=True)
        return active[offset : offset + limit]

    async def update_status(self, session_id: str, status: SessionStatus) -> None:
        if session_id in self.sessions:
            self.sessions[session_id].status = status

    async def delete(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)


# ======================================================================
# Fixtures
# ======================================================================

@pytest.fixture
def repo() -> MemorySessionRepository:
    return MemorySessionRepository()


@pytest.fixture
def mgr(repo: MemorySessionRepository) -> SessionManager:
    return SessionManager(repository=repo)


@pytest.fixture
def mgr_memory_only() -> SessionManager:
    return SessionManager(repository=None)  # no persistence


# ======================================================================
# Create
# ======================================================================

class TestCreate:
    async def test_single_session_defaults(self, mgr: SessionManager):
        s = await mgr.create()
        assert s.id is not None
        assert s.title == "New Chat"
        assert s.type == SessionType.SINGLE
        assert s.status == SessionStatus.ACTIVE
        assert s.participants == []

    async def test_single_session_custom_title(self, mgr: SessionManager):
        s = await mgr.create(title="Debug Session")
        assert s.title == "Debug Session"

    async def test_group_session(self, mgr: SessionManager):
        s = await mgr.create(
            title="Team Room",
            session_type=SessionType.GROUP,
            participants=["alice", "claude", "codex"],
        )
        assert s.type == SessionType.GROUP
        assert len(s.participants) == 3

    async def test_group_session_requires_two_participants(self, mgr: SessionManager):
        with pytest.raises(SessionError, match="2 participants"):
            await mgr.create(
                session_type=SessionType.GROUP,
                participants=["alice"],  # only one
            )

    async def test_create_persists_to_repo(self, mgr: SessionManager, repo: MemorySessionRepository):
        s = await mgr.create(title="Persist Me")
        assert s.id in repo.sessions

    async def test_create_memory_only(self, mgr_memory_only: SessionManager):
        s = await mgr_memory_only.create(title="Ephemeral")
        assert s.status == SessionStatus.ACTIVE
        # Should still work, just not persisted


# ======================================================================
# Read
# ======================================================================

class TestRead:
    async def test_get_existing(self, mgr: SessionManager):
        s = await mgr.create(title="Test")
        fetched = await mgr.get(s.id)
        assert fetched.id == s.id
        assert fetched.title == "Test"

    async def test_get_not_found_raises(self, mgr: SessionManager):
        with pytest.raises(SessionNotFoundError):
            await mgr.get("nonexistent-id")

    async def test_list_active_sorted_by_updated(self, mgr: SessionManager):
        s1 = await mgr.create(title="First")
        s2 = await mgr.create(title="Second")
        # Update s1 so it becomes more recent
        await mgr.update_title(s1.id, "First Updated")
        active = await mgr.list_active()
        # Most-recently-updated first
        assert active[0].id == s1.id

    async def test_list_active_excludes_archived(self, mgr: SessionManager):
        s1 = await mgr.create(title="Active")
        s2 = await mgr.create(title="To Archive")
        await mgr.archive(s2.id)
        active = await mgr.list_active()
        ids = [s.id for s in active]
        assert s1.id in ids
        assert s2.id not in ids

    async def test_list_active_pagination(self, mgr: SessionManager):
        for i in range(5):
            await mgr.create(title=f"Chat {i}")
        page = await mgr.list_active(limit=3, offset=0)
        assert len(page) == 3


# ======================================================================
# Update
# ======================================================================

class TestUpdate:
    async def test_update_title(self, mgr: SessionManager):
        s = await mgr.create(title="Old")
        updated = await mgr.update_title(s.id, "New Title")
        assert updated.title == "New Title"

    async def test_add_participant(self, mgr: SessionManager):
        s = await mgr.create(participants=["alice"])
        updated = await mgr.add_participant(s.id, "claude")
        assert "claude" in updated.participants

    async def test_add_duplicate_participant_noop(self, mgr: SessionManager):
        s = await mgr.create(participants=["alice"])
        updated = await mgr.add_participant(s.id, "alice")
        assert updated.participants.count("alice") == 1

    async def test_remove_participant(self, mgr: SessionManager):
        s = await mgr.create(
            session_type=SessionType.GROUP,
            participants=["alice", "claude", "codex"],
        )
        updated = await mgr.remove_participant(s.id, "codex")
        assert "codex" not in updated.participants

    async def test_remove_participant_violates_group_min(self, mgr: SessionManager):
        s = await mgr.create(
            session_type=SessionType.GROUP,
            participants=["alice", "claude"],
        )
        with pytest.raises(SessionError, match="2 participants"):
            await mgr.remove_participant(s.id, "claude")


# ======================================================================
# Lifecycle
# ======================================================================

class TestLifecycle:
    async def test_archive(self, mgr: SessionManager):
        s = await mgr.create(title="Temp")
        archived = await mgr.archive(s.id)
        assert archived.status == SessionStatus.ARCHIVED

    async def test_delete(self, mgr: SessionManager):
        s = await mgr.create(title="To Delete")
        await mgr.delete(s.id)
        with pytest.raises(SessionNotFoundError):
            await mgr.get(s.id)

    async def test_delete_removes_from_repo(self, mgr: SessionManager, repo: MemorySessionRepository):
        s = await mgr.create(title="Del")
        await mgr.delete(s.id)
        assert s.id not in repo.sessions


# ======================================================================
# Memory-only mode (no repository)
# ======================================================================

class TestMemoryOnly:
    async def test_create_and_read(self, mgr_memory_only: SessionManager):
        s = await mgr_memory_only.create(title="In-Memory")
        fetched = await mgr_memory_only.get(s.id)
        assert fetched.title == "In-Memory"

    async def test_delete_memory_only(self, mgr_memory_only: SessionManager):
        s = await mgr_memory_only.create()
        await mgr_memory_only.delete(s.id)
        with pytest.raises(SessionNotFoundError):
            await mgr_memory_only.get(s.id)

    async def test_list_active_memory_only(self, mgr_memory_only: SessionManager):
        await mgr_memory_only.create(title="A")
        await mgr_memory_only.create(title="B")
        active = await mgr_memory_only.list_active()
        assert len(active) == 2
