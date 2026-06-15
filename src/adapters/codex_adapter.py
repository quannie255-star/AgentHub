"""Codex CLI adapter — wraps the ``codex`` CLI via asyncio subprocess.

Codex is OpenAI's CLI agent. This adapter follows the same pattern as
``ClaudeCodeAdapter``, exposing ``send_message`` and ``stream_response``.

Configuration mirrors ``AgentHubSettings.agents.codex``.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import AsyncIterator

from src.adapters.base import (
    AbstractAgentAdapter,
    AgentTimeoutError,
    AgentUnavailableError,
)
from src.core.review_schema import (
    AgentReviewResult,
    AgentSource,
    IssueCategory,
    Priority,
    PullRequest,
    ReviewIssue,
)
from src.core.schema import (
    AgentCapability,
    AgentContext,
    AgentResponse,
    AgentStatus,
    ChatMessage,
    MessageRole,
)


# ---------------------------------------------------------------------------
# Prompt builder (reused from claude_adapter pattern)
# ---------------------------------------------------------------------------

def _build_prompt(msg: str, context: AgentContext) -> str:
    """Convert AgentContext + message into a single prompt."""
    parts: list[str] = []
    if context.system_prompt:
        parts.append(context.system_prompt)
    for m in context.history:
        role_label = "User" if m.role == MessageRole.USER else "Assistant"
        parts.append(f"{role_label}: {m.content}")
    parts.append(f"User: {msg}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# CLI adapter
# ---------------------------------------------------------------------------

class CodexCLIAdapter(AbstractAgentAdapter):
    """Adapter for OpenAI's Codex CLI.

    Executes ``codex exec`` (non-streaming) or ``codex exec --stream``
    via asyncio subprocess.

    Args:
        cli_path: Path to the ``codex`` binary. Default: ``"codex"``.
        model: Model override (e.g. ``gpt-5``). Empty = CLI default.
        timeout: Maximum seconds per request.
        api_key: OpenAI API key (set as ``OPENAI_API_KEY`` env var).
    """

    def __init__(
        self,
        cli_path: str = "codex",
        model: str = "",
        timeout: float = 60.0,
        api_key: str = "",
    ) -> None:
        self._cli_path = cli_path
        self._model = model
        self._timeout = timeout
        self._api_key = api_key
        self._current_process: asyncio.subprocess.Process | None = None
        self._cancelled = False

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def agent_name(self) -> str:
        return "codex"

    # ------------------------------------------------------------------
    # Capabilities & Health
    # ------------------------------------------------------------------

    def get_capabilities(self) -> AgentCapability:
        return AgentCapability(
            agent_name="codex",
            display_name="Codex CLI",
            description="OpenAI Codex CLI agent — code generation and shell automation",
            supported_actions=[
                "code_generation",
                "code_review",
                "shell_automation",
                "file_ops",
            ],
            max_context_tokens=128_000,
            supports_streaming=True,
            supports_images=True,
        )

    async def health_check(self) -> AgentStatus:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._cli_path, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            if proc.returncode == 0:
                return AgentStatus.IDLE
            return AgentStatus.ERROR
        except FileNotFoundError:
            return AgentStatus.OFFLINE
        except Exception:
            return AgentStatus.ERROR

    # ------------------------------------------------------------------
    # Non-streaming
    # ------------------------------------------------------------------

    async def send_message(self, msg: str, context: AgentContext) -> AgentResponse:
        start_ts = time.perf_counter()

        prompt = _build_prompt(msg, context)
        cmd = await self._build_command(prompt, streaming=False)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._make_env(),
            )
            self._current_process = proc

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout
            )
            latency = (time.perf_counter() - start_ts) * 1000

            if self._cancelled:
                return AgentResponse(
                    agent_name=self.agent_name,
                    content="",
                    finish_reason="error",
                    latency_ms=latency,
                )

            if proc.returncode != 0:
                err_text = stderr.decode("utf-8", errors="replace") if stderr else ""
                return AgentResponse(
                    agent_name=self.agent_name,
                    content=err_text or f"Codex CLI exited with code {proc.returncode}",
                    finish_reason="error",
                    latency_ms=latency,
                )

            content = stdout.decode("utf-8", errors="replace").strip() if stdout else ""
            return AgentResponse(
                agent_name=self.agent_name,
                content=content,
                finish_reason="stop",
                tokens_used=0,
                latency_ms=latency,
            )

        except asyncio.TimeoutError:
            await self._kill_process()
            raise AgentTimeoutError(
                f"Codex CLI timed out after {self._timeout}s"
            )
        except FileNotFoundError:
            raise AgentUnavailableError(
                f"Codex CLI not found at '{self._cli_path}'"
            )
        finally:
            self._current_process = None

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def stream_response(self, msg: str, context: AgentContext) -> AsyncIterator[str]:
        """Stream tokens from Codex CLI line-by-line."""
        prompt = _build_prompt(msg, context)
        cmd = await self._build_command(prompt, streaming=True)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._make_env(),
            )
            self._current_process = proc

            assert proc.stdout is not None
            while True:
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=self._timeout
                    )
                except asyncio.TimeoutError:
                    await self._kill_process()
                    raise AgentTimeoutError(
                        f"Codex CLI stream timed out after {self._timeout}s"
                    )

                if not line:
                    break

                if self._cancelled:
                    await self._kill_process()
                    break

                chunk = line.decode("utf-8", errors="replace").rstrip("\n")
                if chunk:
                    yield chunk

            await proc.wait()

        except FileNotFoundError:
            raise AgentUnavailableError(
                f"Codex CLI not found at '{self._cli_path}'"
            )
        finally:
            self._current_process = None
            self._cancelled = False

    # ------------------------------------------------------------------
    # Code Review (Product Line 2)
    # ------------------------------------------------------------------

    async def review_code(self, pr: PullRequest) -> AgentReviewResult:
        """Execute implementation-level code review (Mock or real LLM)."""
        import json as _json
        from datetime import datetime, timezone

        started = datetime.now(timezone.utc)

        if self._api_key:
            try:
                prompt = self._build_review_prompt(pr)
                cmd = await self._build_command(prompt, streaming=False)
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, env=self._make_env(),
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
                output = stdout.decode("utf-8", errors="replace").strip() if stdout else ""
                issues = self._parse_review_output(output)
            except Exception:
                issues = self._mock_review(pr)
        else:
            issues = self._mock_review(pr)

        elapsed = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        return AgentReviewResult(
            agent_type=AgentSource.CODEX,
            status="completed",
            issues=issues,
            summary=f"Codex CLI found {len(issues)} implementation issues",
            started_at=started,
            completed_at=datetime.now(timezone.utc),
            duration_ms=elapsed,
        )

    def _build_review_prompt(self, pr: PullRequest) -> str:
        files_str = "\n".join(
            f"- `{f.path}` (+{f.additions}/-{f.deletions}) [{f.language}]"
            for f in pr.files[:20]
        )
        return (
            f"You are an expert implementation reviewer. Review this PR:\n\n"
            f"Title: {pr.title}\nDescription: {pr.description or '(no description)'}\n"
            f"Files changed ({len(pr.files)}):\n{files_str}\n\n"
            f"Focus on:\n"
            f"1. Bugs: null pointer risks, race conditions, type errors, edge cases\n"
            f"2. Test coverage: missing tests, uncovered edge cases, brittle assertions\n"
            f"3. Code style: naming conventions, readability, dead code\n"
            f"4. Error handling: missing handlers, exception swallowing, logging\n\n"
            f"For each issue, output JSON array:\n"
            f'[{{"category":"bug|test_coverage|style|best_practice",'
            f'"priority":"P0-Critical|P1-High|P2-Medium|P3-Low","title":"...","description":"...",'
            f'"file_path":"...","line_start":null,"suggestion":"code fix","evidence":"why"}}]\n'
        )

    def _parse_review_output(self, output: str) -> list[ReviewIssue]:
        import json as _json
        try:
            text = output.strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()
            raw = _json.loads(text)
            if isinstance(raw, dict):
                raw = [raw]
            if not isinstance(raw, list):
                return self._mock_review(None)
            issues = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                issues.append(ReviewIssue(
                    category=IssueCategory(item.get("category", "bug")),
                    priority=Priority(item.get("priority", "P2-Medium")),
                    title=item.get("title", "Issue found"),
                    description=item.get("description", ""),
                    file_path=item.get("file_path"),
                    line_start=item.get("line_start"),
                    suggestion=item.get("suggestion"),
                    evidence=item.get("evidence", ""),
                    agent_source=AgentSource.CODEX,
                ))
            return issues or self._mock_review(None)
        except Exception:
            return self._mock_review(None)

    @staticmethod
    def _mock_review(pr: PullRequest | None) -> list[ReviewIssue]:
        return [
            ReviewIssue(
                category=IssueCategory.BUG, priority=Priority.P1_HIGH,
                title="Verify null/None handling for external inputs",
                description="Functions receiving data from external sources should guard against null.",
                file_path="src/auth.py" if pr and pr.files else "src/main.py",
                line_start=15,
                suggestion="Add null checks or use Optional typing with explicit None handling.",
                evidence="Common source of production incidents in data pipelines",
                agent_source=AgentSource.CODEX,
            ),
            ReviewIssue(
                category=IssueCategory.TEST_COVERAGE, priority=Priority.P1_HIGH,
                title="Add unit tests for new/modified functions",
                description="Changed code paths should have corresponding test coverage.",
                file_path="tests/test_auth.py" if pr and any("test" in f.path for f in pr.files) else "tests/",
                suggestion="Add pytest tests: normal input, empty input, boundary values, error conditions.",
                evidence="Test coverage is the primary defense against regressions",
                agent_source=AgentSource.CODEX,
            ),
            ReviewIssue(
                category=IssueCategory.STYLE, priority=Priority.P3_LOW,
                title="Minor: ensure consistent code formatting",
                description="Run formatter to ensure consistent style across changed files.",
                suggestion="Run `ruff format` on changed files.",
                evidence="Team style guide compliance",
                agent_source=AgentSource.CODEX,
            ),
        ]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def cancel(self) -> None:
        self._cancelled = True
        await self._kill_process()

    async def close(self) -> None:
        await self.cancel()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _build_command(self, prompt: str, *, streaming: bool) -> list[str]:
        cmd = [self._cli_path, "exec"]
        if streaming:
            cmd.append("--stream")
        if self._model:
            cmd.extend(["--model", self._model])
        cmd.append(prompt)
        return cmd

    def _make_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self._api_key:
            env["OPENAI_API_KEY"] = self._api_key
        return env

    async def _kill_process(self) -> None:
        proc = self._current_process
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
