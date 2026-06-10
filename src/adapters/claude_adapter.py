"""Claude Code adapter — wraps the ``claude`` CLI via asyncio subprocess.

Supports both non-streaming (``send_message``) and streaming
(``stream_response`` — line-by-line JSON output from ``claude --print``).

Configuration is read from ``AgentHubSettings.agents.claude`` at construction
time, or can be passed explicitly for testing.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import AsyncIterator

from src.adapters.base import (
    AbstractAgentAdapter,
    AgentTimeoutError,
    AgentUnavailableError,
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
# Prompt builder
# ---------------------------------------------------------------------------

def _build_prompt(msg: str, context: AgentContext) -> str:
    """Convert AgentContext + current message into a single prompt string.

    Format::

        [System Prompt if set]

        [History messages as role: content]

        [Current message]
    """
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

class ClaudeCodeAdapter(AbstractAgentAdapter):
    """Adapter for Anthropic's Claude Code CLI.

    Executes ``claude --print`` (streaming) or ``claude -p`` (non-streaming)
    via asyncio subprocess.

    Args:
        cli_path: Path to the ``claude`` binary. Default: ``"claude"`` (from PATH).
        model: Model override (passed as ``--model`` flag). Empty = use CLI default.
        timeout: Maximum seconds per request.
        api_key: Anthropic API key (set as ``ANTHROPIC_API_KEY`` env var).
    """

    def __init__(
        self,
        cli_path: str = "claude",
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
        return "claude"

    # ------------------------------------------------------------------
    # Capabilities & Health
    # ------------------------------------------------------------------

    def get_capabilities(self) -> AgentCapability:
        return AgentCapability(
            agent_name="claude",
            display_name="Claude Code",
            description="Anthropic Claude Code CLI agent — code generation, review, and file ops",
            supported_actions=[
                "code_generation",
                "code_review",
                "file_ops",
                "web_search",
                "debugging",
            ],
            max_context_tokens=200_000,
            supports_streaming=True,
            supports_images=False,
        )

    async def health_check(self) -> AgentStatus:
        """Check if the claude CLI is available."""
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
                    content=err_text or f"CLI exited with code {proc.returncode}",
                    finish_reason="error",
                    latency_ms=latency,
                )

            content = stdout.decode("utf-8", errors="replace").strip() if stdout else ""
            return AgentResponse(
                agent_name=self.agent_name,
                content=content,
                finish_reason="stop",
                tokens_used=0,  # CLI doesn't always report tokens
                latency_ms=latency,
            )

        except asyncio.TimeoutError:
            await self._kill_process()
            raise AgentTimeoutError(
                f"Claude Code timed out after {self._timeout}s"
            )
        except FileNotFoundError:
            raise AgentUnavailableError(
                f"Claude CLI not found at '{self._cli_path}'"
            )
        finally:
            self._current_process = None

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def stream_response(self, msg: str, context: AgentContext) -> AsyncIterator[str]:
        """Stream tokens from ``claude --print`` line-by-line.

        Yields one string chunk per line of stdout.  If the CLI outputs
        JSON lines (stream-json mode), the caller can parse each chunk.
        """
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
                        f"Claude Code stream timed out after {self._timeout}s"
                    )

                if not line:
                    break  # EOF

                if self._cancelled:
                    await self._kill_process()
                    break

                chunk = line.decode("utf-8", errors="replace").rstrip("\n")
                if chunk:
                    yield chunk

            # Drain stderr after stdout closes
            await proc.wait()

        except FileNotFoundError:
            raise AgentUnavailableError(
                f"Claude CLI not found at '{self._cli_path}'"
            )
        finally:
            self._current_process = None
            self._cancelled = False

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
        """Build the CLI argument list."""
        cmd = [self._cli_path]
        if streaming:
            cmd.extend(["--print", "--output-format", "stream-json"])
        else:
            cmd.extend(["-p"])
        if self._model:
            cmd.extend(["--model", self._model])
        cmd.append(prompt)
        return cmd

    def _make_env(self) -> dict[str, str]:
        """Build environment dict for the subprocess."""
        env = os.environ.copy()
        if self._api_key:
            env["ANTHROPIC_API_KEY"] = self._api_key
        return env

    async def _kill_process(self) -> None:
        proc = self._current_process
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
