"""Message bus — async pub/sub for real-time chat message delivery.

Every ``ChatSession`` has a topic. When a message is published to a session,
all subscribers (agents, UI WebSocket handlers, tool callbacks) are notified
via async iterators.

Design:
  - One ``asyncio.Queue`` per session, created lazily on first subscribe.
  - ``subscribe()`` returns an ``AsyncIterator`` — subscribers ``async for`` over it.
  - ``publish()`` pushes a message to all queues listening on that session.
  - ``unsubscribe()`` removes a subscriber; when a session has zero subscribers
    its queue is cleaned up.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict

from src.core.schema import ChatMessage


class Subscription:
    """Handle returned by ``MessageBus.subscribe()``.

    Hold this to iterate over messages or to unsubscribe later.
    """

    def __init__(self, sub_id: str, session_id: str, queue: asyncio.Queue) -> None:
        self.id = sub_id
        self.session_id = session_id
        self._queue = queue
        self._active = True

    def __aiter__(self):
        return self

    async def __anext__(self) -> ChatMessage:
        while self._active:
            msg = await self._queue.get()
            return msg
        raise StopAsyncIteration

    async def close(self) -> None:
        self._active = False


class MessageBus:
    """Central pub/sub message bus for AgentHub.

    Usage::

        bus = MessageBus()

        # Subscriber (e.g. WebSocket handler)
        sub = await bus.subscribe(session_id="s1")
        async for msg in sub:
            print(f"[{msg.sender}] {msg.content}")

        # Publisher (e.g. API endpoint)
        await bus.publish(ChatMessage(session_id="s1", ...))

        # Cleanup
        await bus.unsubscribe(sub)
    """

    def __init__(self) -> None:
        # session_id -> list[Subscription]
        self._subscribers: dict[str, list[Subscription]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self._queues: dict[str, asyncio.Queue] = {}

    # ------------------------------------------------------------------
    # Subscribe
    # ------------------------------------------------------------------

    async def subscribe(self, session_id: str) -> Subscription:
        """Subscribe to messages for a session.

        Returns a ``Subscription`` that can be used as an async iterator.
        """
        async with self._lock:
            if session_id not in self._queues:
                self._queues[session_id] = asyncio.Queue()

            sub = Subscription(
                sub_id=uuid.uuid4().hex[:12],
                session_id=session_id,
                queue=self._queues[session_id],
            )
            self._subscribers[session_id].append(sub)

        return sub

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    async def publish(self, message: ChatMessage) -> int:
        """Publish a message to all subscribers of its session.

        Returns:
            Number of subscribers that received the message.
        """
        session_id = message.session_id

        async with self._lock:
            subs = list(self._subscribers.get(session_id, []))

        delivered = 0
        for sub in subs:
            if sub._active:
                try:
                    sub._queue.put_nowait(message)
                    delivered += 1
                except asyncio.QueueFull:
                    # Drop for this subscriber — shouldn't happen with
                    # unbounded queues, but guard anyway.
                    pass
        return delivered

    # ------------------------------------------------------------------
    # Unsubscribe
    # ------------------------------------------------------------------

    async def unsubscribe(self, subscription: Subscription) -> None:
        """Remove a subscription and close it."""
        await subscription.close()

        async with self._lock:
            session_id = subscription.session_id
            subs = self._subscribers.get(session_id, [])
            if subscription in subs:
                subs.remove(subscription)
            # Clean up empty session
            if not subs:
                self._subscribers.pop(session_id, None)
                self._queues.pop(session_id, None)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def active_sessions(self) -> int:
        """Number of sessions with at least one subscriber."""
        return len(self._subscribers)

    async def subscriber_count(self, session_id: str) -> int:
        """Number of active subscribers for a session."""
        async with self._lock:
            subs = self._subscribers.get(session_id, [])
            return sum(1 for s in subs if s._active)
