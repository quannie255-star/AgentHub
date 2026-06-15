"""Code Review data models (Product Line 2).

All review-specific Pydantic V2 models live here to avoid impacting the
293 existing tests that depend on ``src/core/schema.py``.

Design rules (from playbook):
  - Pydantic V2 syntax only (no ``class Config``)
  - URL fields use ``str``, never ``HttpUrl``
  - Enum classes use ``(str, Enum)`` for JSON compatibility
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ======================================================================
# Enums
# ======================================================================

class IssueCategory(str, Enum):
    ARCHITECTURE = "architecture"
    SECURITY = "security"
    PERFORMANCE = "performance"
    BUG = "bug"
    STYLE = "style"
    TEST_COVERAGE = "test_coverage"
    MAINTAINABILITY = "maintainability"
    BEST_PRACTICE = "best_practice"


class Priority(str, Enum):
    P0_CRITICAL = "P0-Critical"
    P1_HIGH = "P1-High"
    P2_MEDIUM = "P2-Medium"
    P3_LOW = "P3-Low"


class ReviewStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PASSED = "passed"
    FAILED = "failed"
    AUTO_APPROVED = "auto_approved"


class AgentSource(str, Enum):
    CLAUDE = "claude"
    CODEX = "codex"


# ======================================================================
# Core Review Models
# ======================================================================

class FileChange(BaseModel):
    """A single file changed in a PR."""
    path: str
    change_type: Literal["added", "modified", "deleted"] = "modified"
    additions: int = 0
    deletions: int = 0
    patch: str | None = Field(default=None, description="Unified diff text")
    language: str = Field(default="", description="Inferred programming language")


class PullRequest(BaseModel):
    """PR information — sourced from manual input, GitHub webhook, or chat message."""
    id: str = Field(default_factory=_new_id)
    title: str
    description: str | None = None
    author: str = "unknown"
    branch: str = ""
    target_branch: str = "main"
    repository: str = ""
    files: list[FileChange] = Field(default_factory=list)
    # Computed fields
    change_size: Literal["small", "medium", "large"] = "small"
    change_type: Literal["config", "source", "test", "doc", "mixed"] = "mixed"
    impact_scope: Literal["single_file", "multi_file", "public_api", "core_module"] = "single_file"

    def model_post_init(self, __context) -> None:
        """Compute derived fields after init."""
        total = sum(f.additions + f.deletions for f in self.files)
        if total < 50:
            self.change_size = "small"
        elif total < 200:
            self.change_size = "medium"
        else:
            self.change_size = "large"

        languages = {f.language for f in self.files if f.language}
        test_count = sum(1 for f in self.files if "test" in f.path.lower() or "spec" in f.path.lower())
        config_count = sum(1 for f in self.files if f.language in ("yaml", "json", "toml", "markdown"))
        source_count = len(self.files) - test_count - config_count

        if source_count > len(self.files) * 0.6:
            self.change_type = "source"
        elif test_count > len(self.files) * 0.6:
            self.change_type = "test"
        elif config_count > len(self.files) * 0.6:
            self.change_type = "config"
        elif len(self.files) == 1 and self.files[0].language == "markdown":
            self.change_type = "doc"
        else:
            self.change_type = "mixed"

        if len(self.files) == 1:
            self.impact_scope = "single_file"
        elif "public" in " ".join(f.path for f in self.files).lower():
            self.impact_scope = "public_api"
        elif any("core" in f.path.lower() or "engine" in f.path.lower() for f in self.files):
            self.impact_scope = "core_module"
        else:
            self.impact_scope = "multi_file"

    @property
    def total_changes(self) -> int:
        return sum(f.additions + f.deletions for f in self.files)


class ReviewIssue(BaseModel):
    """A single issue found during code review."""
    id: str = Field(default_factory=_new_id)
    category: IssueCategory
    priority: Priority
    title: str
    description: str = ""
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    suggestion: str | None = Field(default=None, description="Suggested fix with code example")
    evidence: str = Field(default="", description="Why this is an issue")
    agent_source: AgentSource = Field(description="Which agent found this")
    rule_reference: str | None = Field(default=None, description="OWASP/SOLID/team standard reference")

    @property
    def is_critical(self) -> bool:
        return self.priority == Priority.P0_CRITICAL

    @property
    def is_high(self) -> bool:
        return self.priority in (Priority.P0_CRITICAL, Priority.P1_HIGH)


class ReviewScore(BaseModel):
    """Six-dimension review score with quality gate logic."""
    overall: float = Field(default=0.0, ge=0.0, le=10.0)
    architecture: float = Field(default=0.0, ge=0.0, le=10.0)
    security: float = Field(default=0.0, ge=0.0, le=10.0)
    performance: float = Field(default=0.0, ge=0.0, le=10.0)
    test_coverage: float = Field(default=0.0, ge=0.0, le=10.0)
    maintainability: float = Field(default=0.0, ge=0.0, le=10.0)
    critical_count: int = 0
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0

    @property
    def total_issues(self) -> int:
        return self.critical_count + self.high_count + self.medium_count + self.low_count

    @property
    def passes_quality_gate(self) -> bool:
        return (
            self.overall >= 6.0
            and self.security >= 7.0
            and self.performance >= 6.0
            and self.test_coverage >= 5.0
            and self.critical_count == 0
        )

    @property
    def auto_approvable(self) -> bool:
        return (
            self.overall >= 8.5
            and self.critical_count == 0
            and self.high_count <= 1
            and self.total_issues <= 3
        )

    @classmethod
    def from_issues(cls, issues: list[ReviewIssue], base_score: float = 10.0) -> "ReviewScore":
        """Compute score from issue list with configurable penalties."""
        critical = sum(1 for i in issues if i.priority == Priority.P0_CRITICAL)
        high = sum(1 for i in issues if i.priority == Priority.P1_HIGH)
        medium = sum(1 for i in issues if i.priority == Priority.P2_MEDIUM)
        low = sum(1 for i in issues if i.priority == Priority.P3_LOW)

        # Category counts
        arch = sum(1 for i in issues if i.category == IssueCategory.ARCHITECTURE)
        sec = sum(1 for i in issues if i.category == IssueCategory.SECURITY)
        perf = sum(1 for i in issues if i.category == IssueCategory.PERFORMANCE)
        test = sum(1 for i in issues if i.category == IssueCategory.TEST_COVERAGE)
        maint = sum(1 for i in issues if i.category in (
            IssueCategory.MAINTAINABILITY, IssueCategory.STYLE, IssueCategory.BEST_PRACTICE,
        ))

        penalties = {"critical": 3.0, "high": 1.5, "medium": 0.5, "low": 0.1}
        total_penalty = (
            critical * penalties["critical"]
            + high * penalties["high"]
            + medium * penalties["medium"]
            + low * penalties["low"]
        )

        return cls(
            overall=max(0.0, min(10.0, base_score - total_penalty * 0.8)),
            architecture=max(0.0, min(10.0, base_score - arch * 1.5)),
            security=max(0.0, min(10.0, base_score - sec * 2.0)),
            performance=max(0.0, min(10.0, base_score - perf * 1.5)),
            test_coverage=max(0.0, min(10.0, base_score - test * 1.5)),
            maintainability=max(0.0, min(10.0, base_score - maint * 1.0)),
            critical_count=critical,
            high_count=high,
            medium_count=medium,
            low_count=low,
        )


class AgentReviewResult(BaseModel):
    """Result from a single agent's review run."""
    agent_type: AgentSource
    status: Literal["pending", "running", "completed", "failed"] = "pending"
    issues: list[ReviewIssue] = Field(default_factory=list)
    summary: str = ""
    started_at: datetime = Field(default_factory=_now)
    completed_at: datetime | None = None
    duration_ms: int = 0


class ReviewReport(BaseModel):
    """Complete review report — renderable as Markdown."""
    id: str = Field(default_factory=_new_id)
    pr_id: str = ""
    pr_title: str = ""
    status: ReviewStatus = ReviewStatus.PENDING
    agent_results: list[AgentReviewResult] = Field(default_factory=list)
    issues: list[ReviewIssue] = Field(default_factory=list)
    score: ReviewScore | None = None
    executive_summary: str = ""
    architecture_review: str = ""
    implementation_review: str = ""
    security_assessment: str = ""
    performance_analysis: str = ""
    test_coverage_review: str = ""
    action_items: str = ""
    markdown: str = ""
    created_at: datetime = Field(default_factory=_now)

    def render_markdown(self) -> str:
        """Generate full 8-section Markdown report."""
        score = self.score or ReviewScore()
        lines = [
            f"# 🔍 Code Review Report: {self.pr_title}",
            "",
            "## 1. Executive Summary",
            "",
            f"- **Status**: {self.status.value.upper()}",
            f"- **Overall Score**: {score.overall:.1f}/10",
            f"- **Issues Found**: {score.total_issues} "
            f"(🔴{score.critical_count} 🟠{score.high_count} 🟡{score.medium_count} 🟢{score.low_count})",
            f"- **Quality Gate**: {'✅ PASSED' if score.passes_quality_gate else '❌ FAILED'}",
            "",
            self.executive_summary or "_Automated review by Claude Code + Codex CLI._",
            "",
            "### Score Breakdown",
            "",
            "| Dimension | Score |",
            "|-----------|-------|",
            f"| Architecture | {score.architecture:.1f}/10 |",
            f"| Security | {score.security:.1f}/10 |",
            f"| Performance | {score.performance:.1f}/10 |",
            f"| Test Coverage | {score.test_coverage:.1f}/10 |",
            f"| Maintainability | {score.maintainability:.1f}/10 |",
            "",
            "## 2. Architecture Review",
            "",
            self.architecture_review or "_No architecture issues found._",
            "",
            "## 3. Implementation Review",
            "",
            self.implementation_review or "_No implementation issues found._",
            "",
            "## 4. Security Assessment",
            "",
            self.security_assessment or "_No security issues found._",
            "",
            "## 5. Performance Analysis",
            "",
            self.performance_analysis or "_No performance issues found._",
            "",
            "## 6. Test Coverage Review",
            "",
            self.test_coverage_review or "_No test coverage issues found._",
            "",
            "## 7. Action Items",
            "",
        ]
        if self.issues:
            for issue in sorted(self.issues, key=lambda x: x.priority.value):
                icon = {"P0-Critical": "🔴", "P1-High": "🟠", "P2-Medium": "🟡", "P3-Low": "🟢"}.get(issue.priority.value, "⚪")
                lines.append(f"- {icon} **[{issue.priority.value}] {issue.title}**")
                if issue.file_path:
                    loc = f"`{issue.file_path}`"
                    if issue.line_start:
                        loc += f":{issue.line_start}"
                    lines.append(f"  - Location: {loc}  |  Source: `{issue.agent_source.value}`")
                if issue.suggestion:
                    lines.append(f"  - Fix: {issue.suggestion}")
                lines.append("")
        else:
            lines.append("_No action items. Great job!_")
            lines.append("")

        lines.append("## 8. Evidence Trace")
        lines.append("")
        for i, issue in enumerate(self.issues):
            lines.append(f"{i+1}. [{issue.id}] {issue.title} — `{issue.agent_source.value}`")
            if issue.evidence:
                lines.append(f"   {issue.evidence[:200]}")

        self.markdown = "\n".join(lines)
        return self.markdown


# ======================================================================
# Metrics Models
# ======================================================================

class DORAMetrics(BaseModel):
    """DORA core four metrics — prove value to CTO."""
    deployment_frequency: float | None = Field(default=None, description="Deployments per week")
    lead_time_hours: float | None = Field(default=None, description="Change lead time in hours")
    lead_time_pr_review_hours: float | None = Field(default=None, description="PR review cycle time")
    change_failure_rate: float | None = Field(default=None, description="Percentage")
    mttr_hours: float | None = Field(default=None, description="Mean time to recovery in hours")

    @property
    def performance_level(self) -> str:
        scores = 0
        if self.deployment_frequency is not None and self.deployment_frequency >= 5:
            scores += 1
        if self.lead_time_hours is not None and self.lead_time_hours <= 12:
            scores += 1
        if self.change_failure_rate is not None and self.change_failure_rate <= 5:
            scores += 1
        if self.mttr_hours is not None and self.mttr_hours <= 3.6:
            scores += 1
        if scores == 4:
            return "elite"
        elif scores >= 2:
            return "high"
        elif scores >= 1:
            return "medium"
        return "low"


class AgentHubMetrics(BaseModel):
    """AgentHub-specific seven metrics — drive product optimization."""
    total_prs_reviewed: int = 0
    ai_reviewed_prs: int = 0
    ai_issues_found: int = 0
    ai_issues_confirmed: int = 0
    ai_issues_false_positive: int = 0
    ai_fixes_suggested: int = 0
    ai_fixes_adopted: int = 0
    avg_human_review_time_minutes_before: float | None = None
    avg_human_review_time_minutes_after: float | None = None
    single_agent_quality_score: float | None = None
    multi_agent_quality_score: float | None = None
    total_operating_cost_usd: float = 0.0
    claude_reviews_count: int = 0
    codex_reviews_count: int = 0

    # --- Computed properties ---

    @property
    def ai_review_coverage(self) -> float:
        """AI review coverage (target > 90%)."""
        if self.total_prs_reviewed == 0:
            return 0.0
        return self.ai_reviewed_prs / self.total_prs_reviewed

    @property
    def ai_issue_detection_rate(self) -> float:
        """Issue detection rate (target > 85%)."""
        total = self.ai_issues_confirmed + self.ai_issues_false_positive
        if total == 0:
            return 0.0
        return self.ai_issues_confirmed / total

    @property
    def ai_fix_adoption_rate(self) -> float:
        """Fix adoption rate (target > 60%)."""
        if self.ai_fixes_suggested == 0:
            return 0.0
        return self.ai_fixes_adopted / self.ai_fixes_suggested

    @property
    def human_review_time_saved_pct(self) -> float:
        """Human review time saved (target > 75%)."""
        if self.avg_human_review_time_minutes_before is None or self.avg_human_review_time_minutes_before == 0:
            return 0.0
        if self.avg_human_review_time_minutes_after is None:
            return 0.0
        return (self.avg_human_review_time_minutes_before - self.avg_human_review_time_minutes_after) / self.avg_human_review_time_minutes_before

    @property
    def multi_agent_efficiency_gain(self) -> float:
        """Multi-agent efficiency gain (target > 40%)."""
        if self.single_agent_quality_score is None or self.single_agent_quality_score == 0:
            return 0.0
        if self.multi_agent_quality_score is None:
            return 0.0
        return (self.multi_agent_quality_score - self.single_agent_quality_score) / self.single_agent_quality_score

    @property
    def cost_per_review_usd(self) -> float:
        """Cost per review (target < $2)."""
        if self.ai_reviewed_prs == 0:
            return 0.0
        return self.total_operating_cost_usd / self.ai_reviewed_prs

    @property
    def agent_utilization_balance(self) -> float:
        """Agent utilization balance (target < 20%)."""
        total = self.claude_reviews_count + self.codex_reviews_count
        if total == 0:
            return 0.0
        return abs(self.claude_reviews_count - self.codex_reviews_count) / total
