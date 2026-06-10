"""SQLite implementation of MessageRepository + SessionRepository.

Uses ``aiosqlite`` for async, non-blocking I/O.
Tables are created automatically on first use (``ensure_tables()``).

Schema::

    CREATE TABLE sessions (
        id          TEXT PRIMARY KEY,
        title       TEXT NOT NULL,
        type        TEXT NOT NULL,   -- 'single' | 'group'
        status      TEXT NOT NULL,   -- 'active' | 'archived' | 'deleted'
        participants TEXT NOT NULL,  -- JSON array
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        metadata    TEXT NOT NULL    -- JSON object
    );

    CREATE TABLE messages (
        id                TEXT PRIMARY KEY,
        session_id        TEXT NOT NULL,
        role              TEXT NOT NULL,
        sender            TEXT NOT NULL,
        content           TEXT NOT NULL,
        mentioned_agents  TEXT NOT NULL,  -- JSON array
        parent_message_id TEXT,
        created_at        TEXT NOT NULL,
        metadata          TEXT NOT NULL,  -- JSON object
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    );

    CREATE INDEX idx_messages_session ON messages(session_id, created_at DESC);
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import aiosqlite

from src.core.schema import ChatMessage, ChatSession, MessageRole, SessionStatus, SessionType
from src.repository.base import MessageRepository, SessionRepository


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _deserialize_message(row: aiosqlite.Row) -> ChatMessage:
    """Convert a DB row back into a ChatMessage."""
    return ChatMessage(
        id=row["id"],
        session_id=row["session_id"],
        role=MessageRole(row["role"]),
        sender=row["sender"],
        content=row["content"],
        mentioned_agents=json.loads(row["mentioned_agents"]),
        parent_message_id=row["parent_message_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        metadata=json.loads(row["metadata"]),
    )


def _deserialize_session(row: aiosqlite.Row) -> ChatSession:
    """Convert a DB row back into a ChatSession."""
    return ChatSession(
        id=row["id"],
        title=row["title"],
        type=SessionType(row["type"]),
        status=SessionStatus(row["status"]),
        participants=json.loads(row["participants"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        metadata=json.loads(row["metadata"]),
    )


# ======================================================================
# SQLite Repositories
# ======================================================================

class SqliteMessageRepository(MessageRepository):
    """SQLite-backed message persistence."""

    def __init__(self, db_path: str = "data/agenthub.db") -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open (or create) the database and ensure tables exist."""
        if self._conn:
            await self._conn.close()
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._ensure_tables()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _ensure_tables(self) -> None:
        assert self._conn is not None
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id           TEXT PRIMARY KEY,
                title        TEXT NOT NULL,
                type         TEXT NOT NULL,
                status       TEXT NOT NULL,
                participants TEXT NOT NULL DEFAULT '[]',
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL,
                metadata     TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS messages (
                id                TEXT PRIMARY KEY,
                session_id        TEXT NOT NULL,
                role              TEXT NOT NULL,
                sender            TEXT NOT NULL,
                content           TEXT NOT NULL,
                mentioned_agents  TEXT NOT NULL DEFAULT '[]',
                parent_message_id TEXT,
                created_at        TEXT NOT NULL,
                metadata          TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_id, created_at DESC);
        """)
        await self._conn.commit()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SqliteMessageRepository not connected — call await .connect() first")
        return self._conn

    # ------------------------------------------------------------------
    # MessageRepository implementation
    # ------------------------------------------------------------------

    async def save(self, message: ChatMessage) -> None:
        await self.conn.execute(
            """INSERT OR REPLACE INTO messages
               (id, session_id, role, sender, content, mentioned_agents,
                parent_message_id, created_at, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                message.id,
                message.session_id,
                message.role.value,
                message.sender,
                message.content,
                json.dumps(message.mentioned_agents, ensure_ascii=False),
                message.parent_message_id,
                message.created_at.isoformat(),
                json.dumps(message.metadata, ensure_ascii=False),
            ),
        )
        await self.conn.commit()

    async def get_by_session(
        self, session_id: str, limit: int = 50, offset: int = 0
    ) -> list[ChatMessage]:
        cursor = await self.conn.execute(
            """SELECT * FROM messages
               WHERE session_id = ?
               ORDER BY created_at DESC
               LIMIT ? OFFSET ?""",
            (session_id, limit, offset),
        )
        rows = await cursor.fetchall()
        return [_deserialize_message(r) for r in rows]

    async def get_by_id(self, message_id: str) -> ChatMessage | None:
        cursor = await self.conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        )
        row = await cursor.fetchone()
        return _deserialize_message(row) if row else None

    async def delete(self, message_id: str) -> None:
        await self.conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        await self.conn.commit()

    async def delete_by_session(self, session_id: str) -> int:
        cursor = await self.conn.execute(
            "SELECT COUNT(*) as cnt FROM messages WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        count = row["cnt"] if row else 0
        await self.conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        await self.conn.commit()
        return count


# ======================================================================

class SqliteSessionRepository(SessionRepository):
    """SQLite-backed session persistence."""

    def __init__(self, db_path: str = "data/agenthub.db") -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        if self._conn:
            await self._conn.close()
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._ensure_tables()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _ensure_tables(self) -> None:
        assert self._conn is not None
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id           TEXT PRIMARY KEY,
                title        TEXT NOT NULL,
                type         TEXT NOT NULL,
                status       TEXT NOT NULL,
                participants TEXT NOT NULL DEFAULT '[]',
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL,
                metadata     TEXT NOT NULL DEFAULT '{}'
            );
        """)
        await self._conn.commit()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SqliteSessionRepository not connected — call await .connect() first")
        return self._conn

    # ------------------------------------------------------------------
    # SessionRepository implementation
    # ------------------------------------------------------------------

    async def save(self, session: ChatSession) -> None:
        await self.conn.execute(
            """INSERT OR REPLACE INTO sessions
               (id, title, type, status, participants, created_at, updated_at, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session.id,
                session.title,
                session.type.value,
                session.status.value,
                json.dumps(session.participants, ensure_ascii=False),
                session.created_at.isoformat(),
                session.updated_at.isoformat(),
                json.dumps(session.metadata, ensure_ascii=False),
            ),
        )
        await self.conn.commit()

    async def get_by_id(self, session_id: str) -> ChatSession | None:
        cursor = await self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        return _deserialize_session(row) if row else None

    async def list_active(
        self, limit: int = 20, offset: int = 0
    ) -> list[ChatSession]:
        cursor = await self.conn.execute(
            """SELECT * FROM sessions
               WHERE status = 'active'
               ORDER BY updated_at DESC
               LIMIT ? OFFSET ?""",
            (limit, offset),
        )
        rows = await cursor.fetchall()
        return [_deserialize_session(r) for r in rows]

    async def update_status(
        self, session_id: str, status: SessionStatus
    ) -> None:
        await self.conn.execute(
            "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
            (status.value, _now_iso(), session_id),
        )
        await self.conn.commit()

    async def delete(self, session_id: str) -> None:
        await self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await self.conn.commit()
