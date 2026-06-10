"""Unit tests for MessageBus (src/core/message_bus.py)."""

from __future__ import annotations

import asyncio

import pytest

from src.core.message_bus import MessageBus, Subscription
from src.core.schema import ChatMessage, MessageRole


# ======================================================================
# Fixtures
# ======================================================================

@pytest.fixture
def bus() -> MessageBus:
    return MessageBus()


def make_msg(session_id: str = "s1", sender: str = "alice", content: str = "hello") -> ChatMessage:
    return ChatMessage(
        session_id=session_id,
        role=MessageRole.USER,
        sender=sender,
        content=content,
    )


# ======================================================================
# Subscribe / Unsubscribe
# ======================================================================

class TestSubscribe:
    async def test_subscribe_returns_subscription(self, bus: MessageBus):
        sub = await bus.subscribe("s1")
        assert isinstance(sub, Subscription)
        assert sub.session_id == "s1"
        assert sub.id is not None

    async def test_subscribe_different_sessions(self, bus: MessageBus):
        sub1 = await bus.subscribe("s1")
        sub2 = await bus.subscribe("s2")
        assert sub1.session_id != sub2.session_id

    async def test_subscriber_count(self, bus: MessageBus):
        await bus.subscribe("s1")
        await bus.subscribe("s1")
        count = await bus.subscriber_count("s1")
        assert count == 2

    async def test_unsubscribe(self, bus: MessageBus):
        sub = await bus.subscribe("s1")
        await bus.unsubscribe(sub)
        count = await bus.subscriber_count("s1")
        assert count == 0

    async def test_active_sessions_property(self, bus: MessageBus):
        await bus.subscribe("s1")
        await bus.subscribe("s2")
        assert bus.active_sessions == 2
        await bus.subscribe("s2")
        assert bus.active_sessions == 2  # same session, count deduped


# ======================================================================
# Publish
# ======================================================================

class TestPublish:
    async def test_publish_delivers_to_subscriber(self, bus: MessageBus):
        sub = await bus.subscribe("s1")
        msg = make_msg(session_id="s1", content="hi")

        delivered = await bus.publish(msg)
        assert delivered == 1

        # Read from subscriber
        received = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
        assert received.content == "hi"
        assert received.sender == "alice"

    async def test_publish_only_to_correct_session(self, bus: MessageBus):
        sub_s1 = await bus.subscribe("s1")
        sub_s2 = await bus.subscribe("s2")

        delivered = await bus.publish(make_msg(session_id="s1", content="for s1"))
        assert delivered == 1  # only s1 subscriber

        # s2 should not receive anything yet
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sub_s2.__anext__(), timeout=0.3)

    async def test_publish_without_subscribers(self, bus: MessageBus):
        delivered = await bus.publish(make_msg(session_id="no-subs"))
        assert delivered == 0

    async def test_publish_multiple_subscribers(self, bus: MessageBus):
        sub1 = await bus.subscribe("s1")
        sub2 = await bus.subscribe("s1")
        sub3 = await bus.subscribe("s1")

        delivered = await bus.publish(make_msg(session_id="s1", content="broadcast"))
        assert delivered == 3

    async def test_unsubscribed_does_not_receive(self, bus: MessageBus):
        sub = await bus.subscribe("s1")
        await bus.unsubscribe(sub)

        delivered = await bus.publish(make_msg(session_id="s1"))
        assert delivered == 0


# ======================================================================
# Async iteration
# ======================================================================

class TestAsyncIteration:
    async def test_multiple_messages_in_order(self, bus: MessageBus):
        sub = await bus.subscribe("s1")

        await bus.publish(make_msg(session_id="s1", content="first"))
        await bus.publish(make_msg(session_id="s1", content="second"))
        await bus.publish(make_msg(session_id="s1", content="third"))

        m1 = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
        m2 = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
        m3 = await asyncio.wait_for(sub.__anext__(), timeout=1.0)

        assert m1.content == "first"
        assert m2.content == "second"
        assert m3.content == "third"


# ======================================================================
# Edge cases
# ======================================================================

class TestEdgeCases:
    async def test_subscribe_after_publish_receives_future_only(self, bus: MessageBus):
        """Subscribers only receive messages published AFTER they subscribe."""
        await bus.publish(make_msg(session_id="s1", content="before"))
        sub = await bus.subscribe("s1")
        await bus.publish(make_msg(session_id="s1", content="after"))

        received = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
        assert received.content == "after"

    async def test_unsubscribe_idempotent(self, bus: MessageBus):
        sub = await bus.subscribe("s1")
        await bus.unsubscribe(sub)
        # Second unsubscribe should not raise
        await bus.unsubscribe(sub)

    async def test_cleanup_on_zero_subscribers(self, bus: MessageBus):
        sub = await bus.subscribe("s1")
        await bus.unsubscribe(sub)
        assert bus.active_sessions == 0
        assert await bus.subscriber_count("s1") == 0
