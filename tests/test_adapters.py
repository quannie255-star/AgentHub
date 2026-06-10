"""Unit tests for Agent Adapters layer.

Covers:
  - ABC contract enforcement (abstract methods, return type constraints)
  - MockAdapter for testing registry and orchestrator integration
  - ClaudeCodeAdapter (construction, capabilities, prompt building, cancel/close)
  - CodexCLIAdapter (construction, capabilities, health_check when CLI absent)
  - AdapterRegistry (register, get, list, find_by_action, health_check_all)
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from src.adapters.base import (
    AbstractAgentAdapter,
    AgentAdapterError,
    AgentTimeoutError,
    AgentUnavailableError,
)
from src.adapters.claude_adapter import (
    ClaudeCodeAdapter,
    _build_prompt as claude_build_prompt,
)
from src.adapters.codex_adapter import CodexCLIAdapter
from src.adapters.registry import AdapterRegistry, AdapterRegistryError
from src.core.schema import (
    AgentCapability,
    AgentContext,
    AgentResponse,
    AgentStatus,
    ChatMessage,
    MessageRole,
)


# ======================================================================
# Mock Adapter — for testing registry and ABC contract
# ======================================================================

class MockAdapter(AbstractAgentAdapter):
    """Fully implemented mock adapter for integration testing."""

    def __init__(
        self,
        name: str = "mock",
        caps: AgentCapability | None = None,
        canned_response: str = "mock response",
        canned_stream: list[str] | None = None,
        fail_health: bool = False,
    ) -> None:
        self._name = name
        self._caps = caps or AgentCapability(
            agent_name=name,
            display_name="Mock Agent",
            description="A mock agent for testing",
            supported_actions=["mock_action"],
        )
        self._canned_response = canned_response
        self._canned_stream = canned_stream or ["mock", " ", "response"]
        self._fail_health = fail_health
        self.cancel_called = False
        self.close_called = False
        self.messages_sent: list[tuple[str, AgentContext]] = []

    @property
    def agent_name(self) -> str:
        return self._name

    async def send_message(self, msg: str, context: AgentContext) -> AgentResponse:
        self.messages_sent.append((msg, context))
        return AgentResponse(
            agent_name=self._name,
            content=self._canned_response,
            finish_reason="stop",
        )

    async def stream_response(self, msg: str, context: AgentContext) -> AsyncIterator[str]:
        self.messages_sent.append((msg, context))
        for chunk in self._canned_stream:
            yield chunk

    def get_capabilities(self) -> AgentCapability:
        return self._caps

    async def health_check(self) -> AgentStatus:
        if self._fail_health:
            return AgentStatus.ERROR
        return AgentStatus.IDLE

    async def cancel(self) -> None:
        self.cancel_called = True

    async def close(self) -> None:
        self.close_called = True
        await self.cancel()


# ======================================================================
# ABC Contract
# ======================================================================

class TestAbstractAdapter:
    """Verify the ABC enforces its contract."""

    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            AbstractAgentAdapter()  # type: ignore[abstract]

    def test_subclass_must_implement_all_abstracts(self):
        """A partial subclass should fail to instantiate."""

        class PartialAdapter(AbstractAgentAdapter):
            @property
            def agent_name(self) -> str:
                return "partial"

        with pytest.raises(TypeError):
            PartialAdapter()  # type: ignore[abstract]

    def test_mock_adapter_is_valid(self):
        adapter = MockAdapter()
        assert isinstance(adapter, AbstractAgentAdapter)

    def test_stream_response_return_type_is_async_iterator(self):
        """stream_response() must return AsyncIterator[str] — verify at runtime."""
        adapter = MockAdapter()
        stream = adapter.stream_response("hello", AgentContext(session_id="s", message_id="m"))
        assert hasattr(stream, "__aiter__")
        assert hasattr(stream, "__anext__")


# ======================================================================
# MockAdapter
# ======================================================================

class TestMockAdapter:
    async def test_send_message(self):
        adapter = MockAdapter(canned_response="hello world")
        ctx = AgentContext(session_id="s1", message_id="m1")
        resp = await adapter.send_message("hi", ctx)
        assert resp.content == "hello world"
        assert resp.agent_name == "mock"
        assert resp.finish_reason == "stop"
        assert len(adapter.messages_sent) == 1

    async def test_stream_response(self):
        adapter = MockAdapter(canned_stream=["a", "b", "c"])
        ctx = AgentContext(session_id="s1", message_id="m1")
        chunks = []
        async for chunk in adapter.stream_response("hi", ctx):
            chunks.append(chunk)
        assert chunks == ["a", "b", "c"]

    async def test_cancel_sets_flag(self):
        adapter = MockAdapter()
        await adapter.cancel()
        assert adapter.cancel_called

    async def test_close_calls_cancel(self):
        adapter = MockAdapter()
        await adapter.close()
        assert adapter.close_called
        assert adapter.cancel_called

    async def test_health_check_ok(self):
        adapter = MockAdapter()
        status = await adapter.health_check()
        assert status == AgentStatus.IDLE

    async def test_health_check_error(self):
        adapter = MockAdapter(fail_health=True)
        status = await adapter.health_check()
        assert status == AgentStatus.ERROR


# ======================================================================
# ClaudeCodeAdapter
# ======================================================================

class TestClaudeCodeAdapter:
    def test_agent_name(self):
        adapter = ClaudeCodeAdapter()
        assert adapter.agent_name == "claude"

    def test_get_capabilities(self):
        adapter = ClaudeCodeAdapter()
        caps = adapter.get_capabilities()
        assert caps.agent_name == "claude"
        assert "code_generation" in caps.supported_actions
        assert "code_review" in caps.supported_actions
        assert caps.supports_streaming is True
        assert caps.max_context_tokens == 200_000

    def test_default_values(self):
        adapter = ClaudeCodeAdapter()
        assert adapter._cli_path == "claude"
        assert adapter._model == ""
        assert adapter._timeout == 60.0
        assert adapter._api_key == ""

    def test_custom_values(self):
        adapter = ClaudeCodeAdapter(
            cli_path="/usr/local/bin/claude",
            model="claude-opus-4-8",
            timeout=120.0,
            api_key="sk-test",
        )
        assert adapter._cli_path == "/usr/local/bin/claude"
        assert adapter._model == "claude-opus-4-8"
        assert adapter._timeout == 120.0
        assert adapter._api_key == "sk-test"

    async def test_health_check_cli_not_found(self):
        """When CLI doesn't exist, health_check should return OFFLINE."""
        adapter = ClaudeCodeAdapter(cli_path="nonexistent-cli-binary-xyz")
        status = await adapter.health_check()
        assert status == AgentStatus.OFFLINE

    async def test_cancel_idempotent(self):
        adapter = ClaudeCodeAdapter()
        await adapter.cancel()
        await adapter.cancel()  # should not raise

    async def test_close(self):
        adapter = ClaudeCodeAdapter()
        await adapter.close()
        # No subprocess running, should not raise

    async def test_make_env_includes_api_key(self):
        adapter = ClaudeCodeAdapter(api_key="sk-my-key")
        env = adapter._make_env()
        assert env["ANTHROPIC_API_KEY"] == "sk-my-key"

    async def test_make_env_no_api_key(self):
        adapter = ClaudeCodeAdapter(api_key="")
        env = adapter._make_env()
        assert "ANTHROPIC_API_KEY" not in env or env.get("ANTHROPIC_API_KEY", "") == ""

    async def test_build_command_non_streaming(self):
        adapter = ClaudeCodeAdapter(model="claude-sonnet-4-6")
        cmd = await adapter._build_command("hello", streaming=False)
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--model" in cmd
        assert "claude-sonnet-4-6" in cmd
        assert "hello" in cmd

    async def test_build_command_streaming(self):
        adapter = ClaudeCodeAdapter()
        cmd = await adapter._build_command("test", streaming=True)
        assert "--print" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd

    def test_prompt_builder_minimal(self):
        ctx = AgentContext(session_id="s", message_id="m")
        prompt = claude_build_prompt("hello", ctx)
        assert "User: hello" in prompt

    def test_prompt_builder_with_system_prompt(self):
        ctx = AgentContext(
            session_id="s", message_id="m", system_prompt="You are a helpful assistant."
        )
        prompt = claude_build_prompt("hi", ctx)
        assert "You are a helpful assistant." in prompt
        assert "User: hi" in prompt

    def test_prompt_builder_with_history(self):
        history = [
            ChatMessage(
                session_id="s", role=MessageRole.USER, sender="alice", content="first question"
            ),
            ChatMessage(
                session_id="s", role=MessageRole.AGENT, sender="claude", content="first answer"
            ),
        ]
        ctx = AgentContext(session_id="s", message_id="m", history=history)
        prompt = claude_build_prompt("second question", ctx)
        assert "User: first question" in prompt
        assert "Assistant: first answer" in prompt
        assert "User: second question" in prompt


# ======================================================================
# CodexCLIAdapter
# ======================================================================

class TestCodexCLIAdapter:
    def test_agent_name(self):
        adapter = CodexCLIAdapter()
        assert adapter.agent_name == "codex"

    def test_get_capabilities(self):
        adapter = CodexCLIAdapter()
        caps = adapter.get_capabilities()
        assert caps.agent_name == "codex"
        assert "code_generation" in caps.supported_actions
        assert "shell_automation" in caps.supported_actions
        assert caps.supports_images is True
        assert caps.max_context_tokens == 128_000

    def test_default_values(self):
        adapter = CodexCLIAdapter()
        assert adapter._cli_path == "codex"
        assert adapter._timeout == 60.0

    def test_custom_values(self):
        adapter = CodexCLIAdapter(
            cli_path="/opt/codex",
            model="gpt-5",
            timeout=90.0,
            api_key="sk-openai-test",
        )
        assert adapter._cli_path == "/opt/codex"
        assert adapter._model == "gpt-5"
        assert adapter._api_key == "sk-openai-test"

    async def test_health_check_cli_not_found(self):
        adapter = CodexCLIAdapter(cli_path="nonexistent-codex-binary-xyz")
        status = await adapter.health_check()
        assert status == AgentStatus.OFFLINE

    async def test_cancel_idempotent(self):
        adapter = CodexCLIAdapter()
        await adapter.cancel()
        await adapter.cancel()

    async def test_make_env_includes_api_key(self):
        adapter = CodexCLIAdapter(api_key="sk-codex-key")
        env = adapter._make_env()
        assert env["OPENAI_API_KEY"] == "sk-codex-key"

    async def test_build_command_streaming(self):
        adapter = CodexCLIAdapter()
        cmd = await adapter._build_command("test", streaming=True)
        assert "--stream" in cmd

    async def test_build_command_non_streaming(self):
        adapter = CodexCLIAdapter(model="gpt-5")
        cmd = await adapter._build_command("hello", streaming=False)
        assert "--stream" not in cmd
        assert "--model" in cmd
        assert "gpt-5" in cmd


# ======================================================================
# AdapterRegistry
# ======================================================================

class TestAdapterRegistry:
    @pytest.fixture
    async def registry(self) -> AdapterRegistry:
        reg = AdapterRegistry()
        await reg.register(MockAdapter(name="agent-a"))
        await reg.register(MockAdapter(name="agent-b"))
        return reg

    async def test_register_and_get(self, registry: AdapterRegistry):
        adapter = await registry.get("agent-a")
        assert adapter.agent_name == "agent-a"

    async def test_get_not_found_raises(self, registry: AdapterRegistry):
        with pytest.raises(AdapterRegistryError, match="not found"):
            await registry.get("no-such-agent")

    async def test_get_or_none_not_found(self, registry: AdapterRegistry):
        result = await registry.get_or_none("ghost")
        assert result is None

    async def test_register_duplicate_rejected(self, registry: AdapterRegistry):
        with pytest.raises(AdapterRegistryError, match="already registered"):
            await registry.register(MockAdapter(name="agent-a"))

    async def test_register_duplicate_with_replace(self, registry: AdapterRegistry):
        new_adapter = MockAdapter(name="agent-a", canned_response="replaced")
        await registry.register(new_adapter, replace=True)
        adapter = await registry.get("agent-a")
        resp = await adapter.send_message(
            "hi", AgentContext(session_id="s", message_id="m")
        )
        assert resp.content == "replaced"

    async def test_unregister(self, registry: AdapterRegistry):
        await registry.unregister("agent-a")
        assert await registry.get_or_none("agent-a") is None

    async def test_unregister_nonexistent_noop(self, registry: AdapterRegistry):
        await registry.unregister("ghost")  # should not raise

    async def test_list_agents(self, registry: AdapterRegistry):
        names = await registry.list_agents()
        assert set(names) == {"agent-a", "agent-b"}

    async def test_list_capabilities(self, registry: AdapterRegistry):
        caps = await registry.list_capabilities()
        assert len(caps) == 2
        names = {c.agent_name for c in caps}
        assert names == {"agent-a", "agent-b"}

    async def test_find_by_action(self):
        reg = AdapterRegistry()
        cap_a = AgentCapability(
            agent_name="a",
            supported_actions=["code_generation", "debugging"],
        )
        cap_b = AgentCapability(
            agent_name="b",
            supported_actions=["web_search"],
        )
        await reg.register(MockAdapter(name="a", caps=cap_a))
        await reg.register(MockAdapter(name="b", caps=cap_b))

        coders = await reg.find_by_action("code_generation")
        assert coders == ["a"]

        searchers = await reg.find_by_action("web_search")
        assert searchers == ["b"]

        nobody = await reg.find_by_action("nonexistent_action")
        assert nobody == []

    async def test_health_check_all(self):
        reg = AdapterRegistry()
        await reg.register(MockAdapter(name="healthy"))
        await reg.register(MockAdapter(name="sick", fail_health=True))

        results = await reg.health_check_all()
        assert results["healthy"] == AgentStatus.IDLE
        assert results["sick"] == AgentStatus.ERROR

    async def test_empty_registry(self):
        reg = AdapterRegistry()
        assert await reg.list_agents() == []
        assert await reg.list_capabilities() == []
        assert await reg.health_check_all() == {}


# ======================================================================
# Exception hierarchy
# ======================================================================

class TestExceptions:
    def test_agent_adapter_error_base(self):
        with pytest.raises(AgentAdapterError):
            raise AgentAdapterError("base error")

    def test_agent_timeout_error_is_adapter_error(self):
        err = AgentTimeoutError("too slow")
        assert isinstance(err, AgentAdapterError)

    def test_agent_unavailable_error_is_adapter_error(self):
        err = AgentUnavailableError("CLI missing")
        assert isinstance(err, AgentAdapterError)
