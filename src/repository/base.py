"""Repository abstract base classes for storage.

The Repository pattern decouples business logic from the storage backend.
Default implementation: SqliteMessageRepository.
Future: PostgresMessageRepository, RedisMessageRepository, etc.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.core.schema import ChatMessage, ChatSession, SessionStatus


# ======================================================================
# Message Repository
# ======================================================================

class MessageRepository(ABC):
    """Abstract interface for persisting chat messages."""

    @abstractmethod
    async def save(self, message: ChatMessage) -> None:
        """Persist a single chat message."""
        ...

    @abstractmethod
    async def get_by_session(
        self, session_id: str, limit: int = 50, offset: int = 0
    ) -> list[ChatMessage]:
        """Return messages for a session, newest first."""
        ...

    @abstractmethod
    async def get_by_id(self, message_id: str) -> ChatMessage | None:
        """Return a single message by its ID."""
        ...

    @abstractmethod
    async def delete(self, message_id: str) -> None:
        """Delete a single message."""
        ...

    @abstractmethod
    async def delete_by_session(self, session_id: str) -> int:
        """Delete all messages in a session. Returns count deleted."""
        ...


# ======================================================================
# Session Repository
# ======================================================================

class SessionRepository(ABC):
    """Abstract interface for persisting chat sessions."""

    @abstractmethod
    async def save(self, session: ChatSession) -> None:
        """Create or update a session."""
        ...

    @abstractmethod
    async def get_by_id(self, session_id: str) -> ChatSession | None:
        """Return a single session by ID."""
        ...

    @abstractmethod
    async def list_active(
        self, limit: int = 20, offset: int = 0
    ) -> list[ChatSession]:
        """Return active sessions, most-recently-updated first."""
        ...

    @abstractmethod
    async def update_status(
        self, session_id: str, status: SessionStatus
    ) -> None:
        """Change the status of a session."""
        ...

    @abstractmethod
    async def delete(self, session_id: str) -> None:
        """Permanently delete a session and all its messages."""
        ...
