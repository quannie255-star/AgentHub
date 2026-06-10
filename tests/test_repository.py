"""Unit tests for SQLite repository implementations.

Covers:
  - SqliteMessageRepository (save, get_by_session, get_by_id, delete, delete_by_session)
  - SqliteSessionRepository (save, get_by_id, list_active, update_status, delete)
  - Connection lifecycle (connect / close)
  - Data round-trip fidelity
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.core.schema import (
    ChatMessage,
    ChatSession,
    MessageRole,
    SessionStatus,
    SessionType,
)
from src.repository.sqlite_repository import SqliteMessageRepository, SqliteSessionRepository


# ======================================================================
# Fixtures
# ======================================================================

@pytest.fixture
def db_path() -> str:
    """Create a temporary SQLite file for isolated tests."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        pass
    path = f.name
    yield path
    # Cleanup
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


@pytest.fixture
async def msg_repo(db_path: str) -> SqliteMessageRepository:
    repo = SqliteMessageRepository(db_path)
    await repo.connect()
    yield repo
    await repo.close()


@pytest.fixture
async def session_repo(db_path: str) -> SqliteSessionRepository:
    repo = SqliteSessionRepository(db_path)
    await repo.connect()
    yield repo
    await repo.close()


# ======================================================================
# Helpers
# ======================================================================

def make_msg(session_id: str = "s1", sender: str = "alice", content: str = "hello") -> ChatMessage:
    return ChatMessage(
        session_id=session_id,
        role=MessageRole.USER,
        sender=sender,
        content=content,
    )


def make_session(title: str = "Test", session_type: SessionType = SessionType.SINGLE) -> ChatSession:
    return ChatSession(title=title, type=session_type, participants=["alice"])


# ======================================================================
# SqliteMessageRepository
# ======================================================================

class TestSqliteMessageRepository:
    async def test_save_and_retrieve(self, msg_repo: SqliteMessageRepository):
        msg = make_msg(session_id="s1", content="hello world")
        await msg_repo.save(msg)

        fetched = await msg_repo.get_by_id(msg.id)
        assert fetched is not None
        assert fetched.content == "hello world"
        assert fetched.sender == "alice"
        assert fetched.role == MessageRole.USER

    async def test_get_by_session_ordering(self, msg_repo: SqliteMessageRepository):
        """Messages should be returned newest-first."""
        import asyncio

        for content in ["first", "second", "third"]:
            msg = make_msg(session_id="s1", content=content)
            await msg_repo.save(msg)
            await asyncio.sleep(0.015)  # ensure distinct timestamps on Windows

        results = await msg_repo.get_by_session("s1")
        assert len(results) == 3
        # Newest first
        assert results[0].content == "third"
        assert results[2].content == "first"

    async def test_get_by_session_limit_offset(self, msg_repo: SqliteMessageRepository):
        for i in range(5):
            await msg_repo.save(make_msg(session_id="s1", content=f"msg-{i}"))

        page = await msg_repo.get_by_session("s1", limit=2, offset=2)
        assert len(page) == 2

    async def test_get_by_id_not_found(self, msg_repo: SqliteMessageRepository):
        result = await msg_repo.get_by_id("nonexistent")
        assert result is None

    async def test_delete_single(self, msg_repo: SqliteMessageRepository):
        msg = make_msg(session_id="s1")
        await msg_repo.save(msg)
        await msg_repo.delete(msg.id)
        assert await msg_repo.get_by_id(msg.id) is None

    async def test_delete_by_session(self, msg_repo: SqliteMessageRepository):
        for _ in range(3):
            await msg_repo.save(make_msg(session_id="s_batch"))
        count = await msg_repo.delete_by_session("s_batch")
        assert count == 3
        remaining = await msg_repo.get_by_session("s_batch")
        assert len(remaining) == 0

    async def test_mentioned_agents_roundtrip(self, msg_repo: SqliteMessageRepository):
        msg = ChatMessage(
            session_id="s1",
            role=MessageRole.USER,
            sender="alice",
            content="@claude @codex fix this",
            mentioned_agents=["claude", "codex"],
        )
        await msg_repo.save(msg)
        fetched = await msg_repo.get_by_id(msg.id)
        assert fetched is not None
        assert fetched.mentioned_agents == ["claude", "codex"]

    async def test_parent_message_id(self, msg_repo: SqliteMessageRepository):
        parent = make_msg(session_id="s1", content="parent")
        await msg_repo.save(parent)
        child = ChatMessage(
            session_id="s1",
            role=MessageRole.AGENT,
            sender="claude",
            content="reply",
            parent_message_id=parent.id,
        )
        await msg_repo.save(child)
        fetched = await msg_repo.get_by_id(child.id)
        assert fetched is not None
        assert fetched.parent_message_id == parent.id

    async def test_metadata_roundtrip(self, msg_repo: SqliteMessageRepository):
        msg = ChatMessage(
            session_id="s1",
            role=MessageRole.SYSTEM,
            sender="system",
            content="meta test",
            metadata={"key": "value", "nested": {"a": 1}},
        )
        await msg_repo.save(msg)
        fetched = await msg_repo.get_by_id(msg.id)
        assert fetched is not None
        assert fetched.metadata == {"key": "value", "nested": {"a": 1}}


# ======================================================================
# SqliteSessionRepository
# ======================================================================

class TestSqliteSessionRepository:
    async def test_save_and_retrieve(self, session_repo: SqliteSessionRepository):
        s = make_session(title="My Session")
        await session_repo.save(s)

        fetched = await session_repo.get_by_id(s.id)
        assert fetched is not None
        assert fetched.title == "My Session"
        assert fetched.type == SessionType.SINGLE
        assert fetched.status == SessionStatus.ACTIVE

    async def test_get_by_id_not_found(self, session_repo: SqliteSessionRepository):
        result = await session_repo.get_by_id("no-such-session")
        assert result is None

    async def test_list_active(self, session_repo: SqliteSessionRepository):
        s1 = make_session(title="Active 1")
        s2 = make_session(title="Active 2")
        await session_repo.save(s1)
        await session_repo.save(s2)
        # Archive s2
        await session_repo.update_status(s2.id, SessionStatus.ARCHIVED)

        active = await session_repo.list_active()
        ids = [s.id for s in active]
        assert s1.id in ids
        assert s2.id not in ids

    async def test_list_active_ordering(self, session_repo: SqliteSessionRepository):
        import asyncio

        s1 = make_session(title="First")
        await session_repo.save(s1)
        await asyncio.sleep(0.015)  # ensure distinct timestamps on Windows
        s2 = make_session(title="Second")
        await session_repo.save(s2)

        active = await session_repo.list_active()
        # Most-recently-updated first
        assert active[0].title == "Second"

    async def test_update_status(self, session_repo: SqliteSessionRepository):
        s = make_session()
        await session_repo.save(s)
        await session_repo.update_status(s.id, SessionStatus.ARCHIVED)

        fetched = await session_repo.get_by_id(s.id)
        assert fetched is not None
        assert fetched.status == SessionStatus.ARCHIVED

    async def test_delete(self, session_repo: SqliteSessionRepository):
        s = make_session()
        await session_repo.save(s)
        await session_repo.delete(s.id)
        assert await session_repo.get_by_id(s.id) is None

    async def test_group_session_participants(self, session_repo: SqliteSessionRepository):
        s = ChatSession(
            title="Team",
            type=SessionType.GROUP,
            participants=["alice", "claude", "codex"],
        )
        await session_repo.save(s)
        fetched = await session_repo.get_by_id(s.id)
        assert fetched is not None
        assert fetched.participants == ["alice", "claude", "codex"]

    async def test_metadata_roundtrip(self, session_repo: SqliteSessionRepository):
        s = ChatSession(title="Meta", metadata={"pinned": True, "color": "blue"})
        await session_repo.save(s)
        fetched = await session_repo.get_by_id(s.id)
        assert fetched is not None
        assert fetched.metadata == {"pinned": True, "color": "blue"}


# ======================================================================
# Connection lifecycle
# ======================================================================

class TestConnectionLifecycle:
    async def test_connect_twice_no_error(self, db_path: str):
        """Second connect on same repo should not fail."""
        repo = SqliteMessageRepository(db_path)
        await repo.connect()
        # Connecting again before closing is safe — just overwrites conn
        await repo.connect()
        # Should work
        msg = make_msg()
        await repo.save(msg)
        await repo.close()

    async def test_close_idempotent(self, db_path: str):
        repo = SqliteMessageRepository(db_path)
        await repo.connect()
        await repo.close()
        await repo.close()  # should not raise

    async def test_use_before_connect_raises(self, db_path: str):
        repo = SqliteMessageRepository(db_path)
        with pytest.raises(RuntimeError, match="not connected"):
            await repo.save(make_msg())


# ======================================================================
# Cross-repo: message FK to session
# ======================================================================

class TestCrossRepo:
    async def test_session_and_message_coexist(self, db_path: str):
        """Messages referencing valid session IDs should be stored correctly."""
        s_repo = SqliteSessionRepository(db_path)
        m_repo = SqliteMessageRepository(db_path)
        await s_repo.connect()
        await m_repo.connect()

        s = make_session()
        await s_repo.save(s)
        msg = make_msg(session_id=s.id)
        await m_repo.save(msg)

        # Both should be retrievable
        assert await m_repo.get_by_id(msg.id) is not None
        assert await s_repo.get_by_id(s.id) is not None

        # Delete session — messages can remain (no FK)
        await s_repo.delete(s.id)
        fetched_msg = await m_repo.get_by_id(msg.id)
        # Message still exists (no cascade — repos are independent)
        assert fetched_msg is not None

        await m_repo.close()
        await s_repo.close()
