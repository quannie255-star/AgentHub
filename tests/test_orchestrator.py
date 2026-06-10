"""Unit tests for Orchestrator layer.

Covers:
  - TaskParser: mention extraction, decomposition strategies, LLM fallback
  - AgentRouter: keyword matching, agent assignment, retry/fallback/timeout
  - Orchestrator: full pipeline, aggregation, status derivation, message bus
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from src.adapters.base import AbstractAgentAdapter
from src.adapters.registry import AdapterRegistry
from src.core.message_bus import MessageBus
from src.core.schema import (
    AgentCapability,
    AgentContext,
    AgentResponse,
    AgentStatus,
    ChatMessage,
    MessageRole,
    OrchestrationTask,
    SubTask,
    TaskStatus,
)
from src.orchestrator.agent_router import (
    AgentRouter,
    RoutingResult,
    _infer_action,
    _KEYWORD_ACTION_MAP,
)
from src.orchestrator.orchestrator import Orchestrator
from src.orchestrator.task_parser import (
    TaskParser,
    decompose_comma,
    decompose_numbered,
    extract_mentions,
    strip_mentions,
)


# ======================================================================
# Helpers — Mock Adapter for orchestrator tests
# ======================================================================

class MockOrchAdapter(AbstractAgentAdapter):
    """Mock adapter that returns configurable responses for orchestrator tests."""

    def __init__(
        self,
        name: str = "mock",
        actions: list[str] | None = None,
        canned_response: str = "done",
        fail_count: int = 0,
        hang_seconds: float = 0,
    ) -> None:
        self._name = name
        self._caps = AgentCapability(
            agent_name=name,
            display_name=f"Mock {name}",
            supported_actions=actions or ["code_generation"],
        )
        self._canned = canned_response
        self._fail_count = fail_count
        self._call_count = 0
        self._hang = hang_seconds
        self.cancelled = False

    @property
    def agent_name(self) -> str:
        return self._name

    async def send_message(self, msg: str, context: AgentContext) -> AgentResponse:
        self._call_count += 1
        if self._hang > 0:
            await asyncio.sleep(self._hang)
        if self._call_count <= self._fail_count:
            return AgentResponse(
                agent_name=self._name, content="error", finish_reason="error"
            )
        return AgentResponse(
            agent_name=self._name, content=self._canned, finish_reason="stop"
        )

    async def stream_response(self, msg: str, context: AgentContext) -> AsyncIterator[str]:
        for chunk in self._canned.split():
            yield chunk + " "

    def get_capabilities(self) -> AgentCapability:
        return self._caps

    async def health_check(self) -> AgentStatus:
        return AgentStatus.IDLE

    async def cancel(self) -> None:
        self.cancelled = True


# ======================================================================
# Fixtures
# ======================================================================

@pytest.fixture
async def registry() -> AdapterRegistry:
    reg = AdapterRegistry()
    await reg.register(MockOrchAdapter(name="claude", actions=["code_generation", "code_review", "debugging", "file_ops"]))
    await reg.register(MockOrchAdapter(name="codex", actions=["code_generation", "shell_automation"]))
    return reg


@pytest.fixture
def bus() -> MessageBus:
    return MessageBus()


# ======================================================================
# TaskParser — Mention extraction
# ======================================================================

class TestExtractMentions:
    def test_single(self):
        assert extract_mentions("@claude fix this") == ["claude"]

    def test_multiple(self):
        mentions = extract_mentions("@claude @codex fix bug")
        assert mentions == ["claude", "codex"]

    def test_no_mentions(self):
        assert extract_mentions("just a plain message") == []

    def test_dedup(self):
        mentions = extract_mentions("@claude @claude fix this")
        assert mentions == ["claude"]

    def test_underscore_names(self):
        assert extract_mentions("@my_agent do thing") == ["my_agent"]

    def test_hyphen_names(self):
        assert extract_mentions("@my-agent do thing") == ["my-agent"]

    def test_chinese_text_with_mentions(self):
        mentions = extract_mentions("请 @claude 修复这个bug")
        assert mentions == ["claude"]


class TestStripMentions:
    def test_removes_all_mentions(self):
        assert strip_mentions("@claude fix @codex review") == "fix review"

    def test_plain_text_unchanged(self):
        assert strip_mentions("hello world") == "hello world"

    def test_collapses_whitespace(self):
        assert strip_mentions("@claude   fix   this") == "fix this"


# ======================================================================
# TaskParser — Decomposition
# ======================================================================

class TestDecomposeNumbered:
    def test_numbered_list(self):
        result = decompose_numbered("1. add login\n2. add dashboard\n3. write tests")
        assert result == ["add login", "add dashboard", "write tests"]

    def test_bulleted_list(self):
        result = decompose_numbered("- fix bug\n- add feature")
        assert result == ["fix bug", "add feature"]

    def test_chinese_numbered(self):
        result = decompose_numbered("1、登录页面\n2、仪表盘")
        assert len(result) == 2

    def test_plain_text_fallback(self):
        result = decompose_numbered("just a single task")
        assert result == ["just a single task"]


class TestDecomposeComma:
    def test_comma_split(self):
        result = decompose_comma("fix bug, add feature, write test")
        assert result == ["fix bug", "add feature", "write test"]

    def test_chinese_comma(self):
        result = decompose_comma("修复bug，添加功能")
        assert len(result) == 2

    def test_semicolons(self):
        result = decompose_comma("task A; task B")
        assert result == ["task A", "task B"]

    def test_single_item(self):
        result = decompose_comma("single task")
        assert result == ["single task"]


# ======================================================================
# TaskParser — Main
# ======================================================================

class TestTaskParser:
    async def test_simple_message(self):
        parser = TaskParser()
        tasks = await parser.parse("fix the login bug")
        assert len(tasks) == 1
        assert tasks[0].description == "fix the login bug"

    async def test_numbered_list(self):
        parser = TaskParser()
        tasks = await parser.parse("1. add login\n2. add dashboard")
        assert len(tasks) == 2
        assert tasks[0].description == "add login"

    async def test_with_mentions(self):
        parser = TaskParser()
        tasks = await parser.parse("@claude fix the bug")
        assert len(tasks) == 1
        # mentions should be stripped from description
        assert "@claude" not in tasks[0].description
        assert "fix the bug" in tasks[0].description

    async def test_comma_separated(self):
        parser = TaskParser()
        tasks = await parser.parse("fix bug A, add feature B, write test C")
        assert len(tasks) == 3

    async def test_llm_decompose(self):
        """When LLM decompose is provided, it should be used as fallback after regex."""

        async def mock_llm(_text: str) -> list[str]:
            return ["llm-task-1", "llm-task-2"]

        parser = TaskParser(llm_decompose=mock_llm)
        tasks = await parser.parse("complex thing that has no list structure")
        assert len(tasks) == 2
        assert tasks[0].description == "llm-task-1"

    async def test_llm_decompose_error_fallback(self):
        """When LLM raises, fall back to single-item."""

        async def failing_llm(_text: str) -> list[str]:
            raise RuntimeError("LLM unavailable")

        parser = TaskParser(llm_decompose=failing_llm)
        tasks = await parser.parse("just one thing")
        assert len(tasks) == 1


# ======================================================================
# AgentRouter — Keyword matching
# ======================================================================

class TestInferAction:
    def test_code_keywords(self):
        assert _infer_action("write code") == "code_generation"
        assert _infer_action("generate a component") == "code_generation"
        assert _infer_action("build the app") == "code_generation"

    def test_review_keywords(self):
        assert _infer_action("review this PR") == "code_review"
        assert _infer_action("audit the changes") == "code_review"

    def test_debug_keywords(self):
        assert _infer_action("fix the bug") == "debugging"
        assert _infer_action("debug the crash") == "debugging"

    def test_deploy_keywords(self):
        assert _infer_action("deploy to prod") == "shell_automation"
        assert _infer_action("docker compose up") == "shell_automation"

    def test_search_keywords(self):
        assert _infer_action("search for docs") == "web_search"

    def test_file_keywords(self):
        assert _infer_action("write file") == "file_ops"
        assert _infer_action("read the config") == "file_ops"

    def test_no_match(self):
        assert _infer_action("do something mysterious") is None


# ======================================================================
# AgentRouter — Main
# ======================================================================

class TestAgentRouter:
    @pytest.fixture
    async def router(self, registry: AdapterRegistry) -> AgentRouter:
        return AgentRouter(registry=registry, default_timeout=5.0, max_retries=2)

    async def test_assign(self, router: AgentRouter):
        st = [SubTask(description="fix the login bug")]
        result = await router.assign(st)
        assert result[0].assigned_agent is not None
        # "fix" → debugging → agent with debugging action
        assert result[0].assigned_agent == "claude"  # claude has debugging

    async def test_assign_falls_back_to_first_agent(self, router: AgentRouter):
        st = [SubTask(description="unknown mystery task")]
        result = await router.assign(st)
        assert result[0].assigned_agent is not None

    async def test_execute_success(self, router: AgentRouter, registry: AdapterRegistry):
        st = SubTask(description="write code", assigned_agent="claude")
        task = OrchestrationTask(session_id="s1", description="test", sub_tasks=[st])

        results = await router.execute(task)
        assert len(results) == 1
        assert results[0].sub_task.status == TaskStatus.SUCCESS
        assert results[0].sub_task.result == "done"

    async def test_execute_retry_on_failure(self, registry: AdapterRegistry):
        """Agent fails twice, succeeds on third attempt."""
        adapter = MockOrchAdapter(name="claude", fail_count=2, canned_response="fixed!")
        reg = AdapterRegistry()
        await reg.register(adapter)
        router = AgentRouter(registry=reg, default_timeout=5.0, max_retries=3)

        st = SubTask(description="fix bug", assigned_agent="claude")
        task = OrchestrationTask(session_id="s", description="test", sub_tasks=[st], retries=3)

        results = await router.execute(task)
        assert results[0].sub_task.status == TaskStatus.SUCCESS
        assert results[0].attempts == 3  # 2 failures + 1 success
        assert results[0].sub_task.result == "fixed!"

    async def test_execute_fallback_on_exhaustion(self, registry: AdapterRegistry):
        """Primary agent always fails, fallback agent succeeds."""
        primary = MockOrchAdapter(name="claude", fail_count=99)  # always fails
        fallback = MockOrchAdapter(name="codex", canned_response="fallback saved it")
        reg = AdapterRegistry()
        await reg.register(primary)
        await reg.register(fallback)
        router = AgentRouter(registry=reg, default_timeout=5.0, max_retries=2)

        st = SubTask(description="fix bug", assigned_agent="claude")
        task = OrchestrationTask(
            session_id="s", description="test", sub_tasks=[st],
            retries=2, fallback_agent="codex",
        )

        results = await router.execute(task)
        assert results[0].sub_task.status == TaskStatus.SUCCESS
        assert results[0].agent_name == "codex"
        assert results[0].used_fallback is True
        assert results[0].sub_task.result == "fallback saved it"

    async def test_execute_total_failure(self, registry: AdapterRegistry):
        """Both primary and fallback fail."""
        primary = MockOrchAdapter(name="claude", fail_count=99)
        fallback = MockOrchAdapter(name="codex", fail_count=99)
        reg = AdapterRegistry()
        await reg.register(primary)
        await reg.register(fallback)
        router = AgentRouter(registry=reg, default_timeout=5.0, max_retries=1)

        st = SubTask(description="fix bug", assigned_agent="claude")
        task = OrchestrationTask(
            session_id="s", description="test", sub_tasks=[st],
            retries=1, fallback_agent="codex",
        )

        results = await router.execute(task)
        assert results[0].sub_task.status == TaskStatus.FAILED
        assert results[0].error is not None

    async def test_execute_timeout(self, registry: AdapterRegistry):
        """Agent hangs, should be caught by timeout."""
        slow = MockOrchAdapter(name="claude", hang_seconds=10)
        reg = AdapterRegistry()
        await reg.register(slow)
        router = AgentRouter(registry=reg, default_timeout=0.1, max_retries=1)

        st = SubTask(description="do thing", assigned_agent="claude")
        task = OrchestrationTask(
            session_id="s", description="test", sub_tasks=[st],
            retries=1, timeout=0.05,
        )

        results = await router.execute(task)
        assert results[0].sub_task.status == TaskStatus.FAILED

    async def test_no_agent_assigned(self, registry: AdapterRegistry):
        """Sub-task with no assigned_agent should fail immediately."""
        router = AgentRouter(registry=registry, default_timeout=5.0, max_retries=2)
        st = SubTask(description="do thing", assigned_agent=None)
        task = OrchestrationTask(session_id="s", description="test", sub_tasks=[st])

        results = await router.execute(task)
        assert results[0].sub_task.status == TaskStatus.FAILED
        assert "No agent assigned" in (results[0].error or "")


# ======================================================================
# Orchestrator
# ======================================================================

class TestOrchestrator:
    @pytest.fixture
    async def orch(self, registry: AdapterRegistry, bus: MessageBus) -> Orchestrator:
        return Orchestrator(
            registry=registry,
            message_bus=bus,
            default_timeout=5.0,
            default_retries=1,
            fallback_agent="codex",
        )

    async def test_run_simple(self, orch: Orchestrator):
        result = await orch.run(session_id="s1", user_message="fix the login bug")
        assert result.status == TaskStatus.SUCCESS
        assert len(result.sub_tasks) == 1
        assert result.sub_tasks[0].status == TaskStatus.SUCCESS
        assert result.final_result is not None
        assert "login" in result.final_result

    async def test_run_numbered_list(self, orch: Orchestrator):
        result = await orch.run(
            session_id="s1",
            user_message="1. add login page\n2. add dashboard\n3. write tests",
        )
        assert len(result.sub_tasks) == 3

    async def test_run_with_mentions(self, orch: Orchestrator):
        result = await orch.run(
            session_id="s1",
            user_message="@claude fix the login bug",
        )
        # Claude should be assigned
        assigned = [st.assigned_agent for st in result.sub_tasks]
        assert "claude" in assigned

    async def test_run_publishes_to_bus(self, orch: Orchestrator, bus: MessageBus):
        sub = await bus.subscribe("s1")
        await orch.run(session_id="s1", user_message="fix something")

        # Should receive the orchestrator's result message
        try:
            msg = await asyncio.wait_for(sub.__anext__(), timeout=2.0)
            assert msg.role == MessageRole.SYSTEM
            assert msg.sender == "orchestrator"
            assert msg.metadata.get("task_id") is not None
        finally:
            await bus.unsubscribe(sub)

    async def test_run_empty_message(self, orch: Orchestrator):
        result = await orch.run(session_id="s1", user_message="")
        assert result.status == TaskStatus.SUCCESS
        assert result.final_result is not None

    async def test_run_with_history(self, orch: Orchestrator):
        history = [
            ChatMessage(session_id="s1", role=MessageRole.USER, sender="alice", content="previous question"),
        ]
        result = await orch.run(
            session_id="s1",
            user_message="fix bug",
            history=history,
            system_prompt="You are a senior engineer.",
        )
        assert result.status == TaskStatus.SUCCESS


# ======================================================================
# Orchestrator — Aggregation & Status
# ======================================================================

class TestAggregation:
    def test_all_success(self):
        results = [
            RoutingResult(
                sub_task=SubTask(description="task 1", status=TaskStatus.SUCCESS, result="done 1"),
                agent_name="claude",
                response=AgentResponse(agent_name="claude", content="done 1"),
            ),
            RoutingResult(
                sub_task=SubTask(description="task 2", status=TaskStatus.SUCCESS, result="done 2"),
                agent_name="codex",
                response=AgentResponse(agent_name="codex", content="done 2"),
            ),
        ]
        agg = Orchestrator._aggregate(results)
        assert "task 1" in agg
        assert "task 2" in agg
        assert "claude" in agg
        assert "codex" in agg

    def test_all_failed(self):
        results = [
            RoutingResult(
                sub_task=SubTask(description="task 1", status=TaskStatus.FAILED),
                error="boom",
            ),
        ]
        agg = Orchestrator._aggregate(results)
        assert "FAILED" in agg
        assert "boom" in agg

    def test_derive_all_success(self):
        results = [
            RoutingResult(sub_task=SubTask(description="t1", status=TaskStatus.SUCCESS)),
            RoutingResult(sub_task=SubTask(description="t2", status=TaskStatus.SUCCESS)),
        ]
        assert Orchestrator._derive_status(results) == TaskStatus.SUCCESS

    def test_derive_all_failed(self):
        results = [
            RoutingResult(sub_task=SubTask(description="t1", status=TaskStatus.FAILED)),
        ]
        assert Orchestrator._derive_status(results) == TaskStatus.FAILED

    def test_derive_partial(self):
        results = [
            RoutingResult(sub_task=SubTask(description="t1", status=TaskStatus.SUCCESS)),
            RoutingResult(sub_task=SubTask(description="t2", status=TaskStatus.FAILED)),
        ]
        assert Orchestrator._derive_status(results) == TaskStatus.FAILED

    def test_derive_empty(self):
        assert Orchestrator._derive_status([]) == TaskStatus.SUCCESS


# ======================================================================
# OrchestrationTask robustness fields
# ======================================================================

class TestRobustnessFields:
    """Verify that OrchestrationTask carries the mandated robustness fields."""

    def test_defaults(self):
        task = OrchestrationTask(session_id="s1", description="test")
        assert task.retries == 3
        assert task.timeout == 60.0
        assert task.fallback_agent is None

    def test_override_from_orchestrator(self, registry: AdapterRegistry):
        orch = Orchestrator(
            registry=registry,
            default_timeout=30.0,
            default_retries=5,
            fallback_agent="codex",
        )
        # These values should be used when creating OrchestrationTask
        assert orch._default_timeout == 30.0
        assert orch._default_retries == 5
        assert orch._fallback_agent == "codex"
