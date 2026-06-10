"""Unit tests for shared data models (src/core/schema.py).

Covers:
  - Model instantiation with defaults
  - Model instantiation with explicit values
  - Enum serialisation (JSON-safe)
  - OrchestrationTask robustness fields (retries / timeout / fallback_agent)
  - AnnotatedFinding + Evidence traceability chain
  - Edge cases (empty lists, None optional fields)
"""

from __future__ import annotations

import json

import pytest

from src.core.schema import (
    AgentCapability,
    AgentContext,
    AgentHeartbeat,
    AgentResponse,
    AgentStatus,
    AnnotatedFinding,
    APIResponse,
    ChatMessage,
    ChatSession,
    DeployResult,
    DiffResult,
    Evidence,
    HealthCheck,
    MessageRole,
    OrchestrationTask,
    PreviewResult,
    SessionStatus,
    SessionType,
    SubTask,
    TaskStatus,
)


# ======================================================================
# Evidence & AnnotatedFinding
# ======================================================================

class TestEvidence:
    def test_defaults(self):
        e = Evidence(source="https://example.com")
        assert e.source == "https://example.com"
        assert e.excerpt == ""
        assert e.timestamp is not None

    def test_with_excerpt(self):
        e = Evidence(source="doc", excerpt="relevant text")
        assert e.excerpt == "relevant text"

    def test_json_roundtrip(self):
        e = Evidence(source="src", excerpt="ex")
        d = json.loads(e.model_dump_json())
        assert d["source"] == "src"
        assert d["excerpt"] == "ex"


class TestAnnotatedFinding:
    def test_minimal(self):
        f = AnnotatedFinding(text="strength: fast")
        assert f.text == "strength: fast"
        assert f.evidence == []

    def test_with_evidence(self):
        ev = Evidence(source="url", excerpt="supports claim")
        f = AnnotatedFinding(text="claim", evidence=[ev])
        assert len(f.evidence) == 1
        assert f.evidence[0].source == "url"

    def test_json_serializable(self):
        f = AnnotatedFinding(text="x", evidence=[Evidence(source="s")])
        d = json.loads(f.model_dump_json())
        assert d["text"] == "x"
        assert len(d["evidence"]) == 1


# ======================================================================
# Chat & Session
# ======================================================================

class TestChatMessage:
    def test_defaults(self):
        m = ChatMessage(session_id="s1", role=MessageRole.USER, sender="alice", content="hi")
        assert m.id is not None
        assert len(m.id) == 12
        assert m.session_id == "s1"
        assert m.role == MessageRole.USER
        assert m.sender == "alice"
        assert m.content == "hi"
        assert m.mentioned_agents == []
        assert m.parent_message_id is None
        assert m.created_at is not None

    def test_with_mentions(self):
        m = ChatMessage(
            session_id="s1",
            role=MessageRole.USER,
            sender="alice",
            content="@claude fix this",
            mentioned_agents=["claude"],
        )
        assert m.mentioned_agents == ["claude"]

    def test_json_roundtrip(self):
        m = ChatMessage(
            session_id="s1",
            role=MessageRole.AGENT,
            sender="claude",
            content="done",
            mentioned_agents=[],
        )
        d = json.loads(m.model_dump_json())
        assert d["role"] == "agent"
        assert d["sender"] == "claude"


class TestChatSession:
    def test_defaults(self):
        s = ChatSession()
        assert s.id is not None
        assert s.type == SessionType.SINGLE
        assert s.status == SessionStatus.ACTIVE
        assert s.participants == []

    def test_group_session(self):
        s = ChatSession(
            title="Bug Hunt",
            type=SessionType.GROUP,
            participants=["alice", "claude", "codex"],
        )
        assert s.type == SessionType.GROUP
        assert len(s.participants) == 3

    def test_json_roundtrip(self):
        s = ChatSession(title="Test", type=SessionType.SINGLE)
        d = json.loads(s.model_dump_json())
        assert d["type"] == "single"


class TestEnums:
    def test_message_role_serialisation(self):
        assert json.dumps(MessageRole.USER) == '"user"'
        assert json.dumps(MessageRole.AGENT) == '"agent"'
        assert json.dumps(MessageRole.SYSTEM) == '"system"'

    def test_session_type_serialisation(self):
        assert json.dumps(SessionType.SINGLE) == '"single"'
        assert json.dumps(SessionType.GROUP) == '"group"'

    def test_task_status_serialisation(self):
        for status in TaskStatus:
            raw = json.dumps(status)
            assert isinstance(raw, str)
            assert status.value in raw


# ======================================================================
# Agent Models
# ======================================================================

class TestAgentCapability:
    def test_full(self):
        cap = AgentCapability(
            agent_name="claude",
            display_name="Claude Code",
            description="Anthropic Claude Code CLI agent",
            supported_actions=["code_generation", "code_review"],
            max_context_tokens=200_000,
            supports_streaming=True,
        )
        assert cap.agent_name == "claude"
        assert cap.supported_actions == ["code_generation", "code_review"]
        assert cap.supports_streaming is True
        assert cap.supports_images is False

    def test_json_roundtrip(self):
        cap = AgentCapability(agent_name="test")
        d = json.loads(cap.model_dump_json())
        assert d["agent_name"] == "test"
        assert "supported_actions" in d


class TestAgentContext:
    def test_minimal(self):
        ctx = AgentContext(session_id="s1", message_id="m1")
        assert ctx.session_id == "s1"
        assert ctx.message_id == "m1"
        assert ctx.history == []
        assert ctx.system_prompt is None

    def test_with_history(self):
        msg = ChatMessage(
            session_id="s1", role=MessageRole.USER, sender="alice", content="hello"
        )
        ctx = AgentContext(session_id="s1", message_id="m1", history=[msg])
        assert len(ctx.history) == 1


class TestAgentResponse:
    def test_success(self):
        r = AgentResponse(
            agent_name="claude",
            content="task completed",
            finish_reason="stop",
            tokens_used=150,
            latency_ms=1200.0,
        )
        assert r.agent_name == "claude"
        assert r.content == "task completed"
        assert r.finish_reason == "stop"

    def test_error_response(self):
        r = AgentResponse(
            agent_name="claude",
            content="",
            finish_reason="error",
        )
        assert r.finish_reason == "error"

    def test_json_roundtrip(self):
        r = AgentResponse(agent_name="test", content="ok")
        d = json.loads(r.model_dump_json())
        assert d["finish_reason"] == "stop"


# ======================================================================
# Orchestration — KEY robustness fields
# ======================================================================

class TestOrchestrationTask:
    """Verify the robustness fields mandated by the revised plan:
    retries, timeout, fallback_agent."""

    def test_defaults(self):
        task = OrchestrationTask(session_id="s1", description="build a login page")
        assert task.retries == 3
        assert task.timeout == 60.0
        assert task.fallback_agent is None
        assert task.status == TaskStatus.PENDING
        assert task.sub_tasks == []

    def test_custom_robustness(self):
        task = OrchestrationTask(
            session_id="s1",
            description="deploy to prod",
            retries=5,
            timeout=120.0,
            fallback_agent="codex",
        )
        assert task.retries == 5
        assert task.timeout == 120.0
        assert task.fallback_agent == "codex"

    def test_with_sub_tasks(self):
        sub = SubTask(description="write tests")
        task = OrchestrationTask(
            session_id="s1",
            description="full pipeline",
            sub_tasks=[sub],
        )
        assert len(task.sub_tasks) == 1
        assert task.sub_tasks[0].description == "write tests"

    def test_json_roundtrip(self):
        task = OrchestrationTask(
            session_id="s1",
            description="test",
            retries=2,
            timeout=30.0,
            fallback_agent="claude",
        )
        d = json.loads(task.model_dump_json())
        assert d["retries"] == 2
        assert d["timeout"] == 30.0
        assert d["fallback_agent"] == "claude"


class TestSubTask:
    def test_defaults(self):
        st = SubTask(description="do something")
        assert st.status == TaskStatus.PENDING
        assert st.dependencies == []
        assert st.assigned_agent is None
        assert st.result is None

    def test_with_deps(self):
        st = SubTask(description="step 2", dependencies=["step-1-id"])
        assert st.dependencies == ["step-1-id"]


class TestAgentHeartbeat:
    def test_defaults(self):
        hb = AgentHeartbeat(agent_name="claude")
        assert hb.agent_name == "claude"
        assert hb.status == AgentStatus.IDLE
        assert hb.current_task_id is None


# ======================================================================
# Tool Outputs
# ======================================================================

class TestDiffResult:
    def test_full(self):
        dr = DiffResult(
            file_path="src/main.py",
            original="print('old')",
            modified="print('new')",
            unified_diff="@@ -1,1 +1,1 @@\n-print('old')\n+print('new')",
            language="python",
        )
        assert dr.file_path == "src/main.py"
        assert dr.language == "python"


class TestPreviewResult:
    def test_running(self):
        pr = PreviewResult(url="http://localhost:9000", port=9000)
        assert pr.status == "running"
        assert pr.url == "http://localhost:9000"


class TestDeployResult:
    def test_deployed(self):
        dr = DeployResult(service_name="agenthub-backend", status="deployed")
        assert dr.service_name == "agenthub-backend"


# ======================================================================
# API Envelope
# ======================================================================

class TestAPIResponse:
    def test_success(self):
        r = APIResponse(data={"key": "value"})
        assert r.success is True
        assert r.data == {"key": "value"}
        assert r.error is None

    def test_error(self):
        r = APIResponse(success=False, error="something went wrong")
        assert r.success is False
        assert r.error == "something went wrong"


class TestHealthCheck:
    def test_defaults(self):
        hc = HealthCheck()
        assert hc.status == "ok"
        assert hc.version == "0.1.0"
        assert hc.agents_online == 0


# ======================================================================
# Cross-cutting: JSON safety (all enums must survive dump/load)
# ======================================================================

def test_all_models_json_roundtrip():
    """Sanity check: every model can be dumped and loaded without error."""
    models = [
        Evidence(source="s"),
        AnnotatedFinding(text="t"),
        ChatMessage(session_id="s", role=MessageRole.USER, sender="u", content="c"),
        ChatSession(title="t"),
        AgentCapability(agent_name="a"),
        AgentContext(session_id="s", message_id="m"),
        AgentResponse(agent_name="a", content="c"),
        AgentHeartbeat(agent_name="a"),
        OrchestrationTask(session_id="s", description="d"),
        SubTask(description="d"),
        DiffResult(file_path="f", original="o", modified="m", unified_diff="d"),
        PreviewResult(url="http://x", port=9000),
        DeployResult(service_name="s", status="deployed"),
        APIResponse(data=None),
        HealthCheck(),
    ]
    for m in models:
        raw = m.model_dump_json()
        back = type(m).model_validate_json(raw)
        assert back == m, f"Round-trip failed for {type(m).__name__}"
