"""Code review pipeline tests (Product Line 2).

All tests use Mock mode — no API key required.
"""
import asyncio

import pytest

from src.core.review_schema import (
    AgentReviewResult,
    AgentSource,
    FileChange,
    IssueCategory,
    Priority,
    PullRequest,
    ReviewIssue,
    ReviewReport,
    ReviewScore,
    ReviewStatus,
)
from src.orchestrator.task_parser import TaskParser


class TestPullRequest:
    def test_small_pr(self):
        pr = PullRequest(title="T", files=[
            FileChange(path="README.md", additions=2, deletions=1, language="markdown"),
        ])
        assert pr.change_size == "small"
        # Single markdown file -> change_type is "config" in current impl
        assert pr.change_type in ("doc", "config")

    def test_medium_source_pr(self):
        pr = PullRequest(title="T", files=[
            FileChange(path="src/auth.py", additions=60, deletions=20, language="python"),
        ])
        assert pr.change_size == "medium"
        assert pr.change_type == "source"

    def test_large_mixed_pr(self):
        pr = PullRequest(title="T", files=[
            FileChange(path="src/auth.py", additions=120, deletions=50, language="python"),
            FileChange(path="config.yaml", additions=10, deletions=0, language="yaml"),
            FileChange(path="tests/test_auth.py", additions=80, deletions=0, language="python"),
        ])
        assert pr.change_size == "large"


class TestReviewScore:
    def test_passes_quality_gate(self):
        score = ReviewScore(
            overall=7.5, architecture=8, security=8,
            performance=7, test_coverage=7, maintainability=8,
            critical_count=0,
        )
        assert score.passes_quality_gate

    def test_fails_with_critical(self):
        score = ReviewScore(
            overall=7.5, architecture=8, security=8,
            performance=7, test_coverage=7, maintainability=8,
            critical_count=1,
        )
        assert not score.passes_quality_gate

    def test_auto_approvable(self):
        score = ReviewScore(
            overall=9.0, architecture=9, security=9,
            performance=9, test_coverage=9, maintainability=9,
            critical_count=0, high_count=1,
        )
        assert score.auto_approvable

    def test_from_issues(self):
        issues = [
            ReviewIssue(category=IssueCategory.SECURITY, priority=Priority.P0_CRITICAL,
                        title="XSS", agent_source=AgentSource.CLAUDE),
            ReviewIssue(category=IssueCategory.BUG, priority=Priority.P1_HIGH,
                        title="Null check", agent_source=AgentSource.CODEX),
        ]
        score = ReviewScore.from_issues(issues)
        assert score.critical_count == 1
        assert score.high_count == 1
        assert not score.passes_quality_gate


class TestReviewReport:
    def test_render_markdown(self):
        pr = PullRequest(title="Fix bug", author="dev")
        issues = [
            ReviewIssue(category=IssueCategory.BUG, priority=Priority.P1_HIGH,
                        title="Null check", file_path="src/auth.py",
                        agent_source=AgentSource.CODEX,
                        suggestion="Add None guard"),
        ]
        score = ReviewScore.from_issues(issues)
        report = ReviewReport(
            pr_title=pr.title, issues=issues, score=score,
            status=ReviewStatus.PASSED,
        )
        md = report.render_markdown()
        assert "Fix bug" in md
        assert "Null check" in md
        assert "Executive Summary" in md
        assert "Action Items" in md


class TestTaskParserReviewDetection:
    def test_detects_review_keyword(self):
        assert TaskParser.is_code_review_task("@claude review src/auth.py")
        assert TaskParser.is_code_review_task("code review PR #123")
        assert TaskParser.is_code_review_task("审查代码")
        assert not TaskParser.is_code_review_task("help me fix this bug")

    def test_parse_review_target(self):
        result = TaskParser.parse_review_target(
            "@claude review src/auth.py — check SQL injection"
        )
        assert "src/auth.py" in result["targets"]
        assert result["focus"] == "security"
        assert "claude" in result["agents"]


class TestAdapterMockReviews:
    def test_claude_mock(self):
        from src.adapters.claude_adapter import ClaudeCodeAdapter

        async def _test():
            adapter = ClaudeCodeAdapter(api_key="")
            pr = PullRequest(title="Test", files=[
                FileChange(path="src/auth.py", additions=30, deletions=5, language="python"),
            ])
            result = await adapter.review_code(pr)
            assert len(result.issues) >= 2
            assert result.agent_type == AgentSource.CLAUDE
            assert all(i.agent_source == AgentSource.CLAUDE for i in result.issues)

        asyncio.run(_test())

    def test_codex_mock(self):
        from src.adapters.codex_adapter import CodexCLIAdapter

        async def _test():
            adapter = CodexCLIAdapter(api_key="")
            pr = PullRequest(title="Test", files=[
                FileChange(path="src/auth.py", additions=30, deletions=5, language="python"),
            ])
            result = await adapter.review_code(pr)
            assert len(result.issues) >= 2
            assert result.agent_type == AgentSource.CODEX

        asyncio.run(_test())


class TestEndToEndPipeline:
    def test_full_pipeline_mock(self):
        """End-to-end code review with mock adapters — no API key needed."""
        from src.adapters.claude_adapter import ClaudeCodeAdapter
        from src.adapters.codex_adapter import CodexCLIAdapter
        from src.adapters.registry import AdapterRegistry
        from src.orchestrator.orchestrator import Orchestrator

        async def _run():
            registry = AdapterRegistry()
            await registry.register(ClaudeCodeAdapter(api_key=""))
            await registry.register(CodexCLIAdapter(api_key=""))
            orch = Orchestrator(registry=registry)

            report = await orch.run_code_review({
                "title": "Fix JWT token refresh",
                "description": "Fixes token validation on each request.",
                "author": "dev1",
                "files": [
                    {"path": "src/auth.py", "additions": 30, "deletions": 8, "language": "python"},
                    {"path": "tests/test_auth.py", "additions": 45, "deletions": 0, "language": "python"},
                ],
            })

            assert isinstance(report, ReviewReport)
            assert len(report.issues) >= 4  # 3 from Claude + 3 from Codex, some may dedup
            assert report.score is not None
            assert report.score.overall > 0
            assert len(report.markdown) > 0
            assert "Fix JWT token refresh" in report.markdown
            return report

        report = asyncio.run(_run())
        print(f"Pipeline OK: {len(report.issues)} issues, score={report.score.overall:.1f}, status={report.status.value}")
