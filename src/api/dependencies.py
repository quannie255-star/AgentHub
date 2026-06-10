"""FastAPI dependency injection — wire singletons into the app.

All long-lived objects (MessageBus, SessionManager, AdapterRegistry,
Orchestrator) are created once and stored on ``app.state`` so route
handlers can access them via ``request.app.state``.

Design:
  - ``create_app()`` calls ``setup_dependencies(app)`` at startup.
  - Route handlers use ``request.app.state.xxx`` directly (no Depends()
    magic to keep things transparent and easy to override in tests).
  - Tests can call ``setup_dependencies(app, ...)`` with mock instances.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from src.adapters.claude_adapter import ClaudeCodeAdapter
from src.adapters.codex_adapter import CodexCLIAdapter
from src.adapters.registry import AdapterRegistry
from src.core.config import get_settings
from src.core.message_bus import MessageBus
from src.core.session import SessionManager
from src.orchestrator.orchestrator import Orchestrator
from src.orchestrator.task_parser import TaskParser

logger = logging.getLogger("agenthub.api")


async def setup_dependencies(
    app: FastAPI,
    *,
    session_manager: SessionManager | None = None,
    message_bus: MessageBus | None = None,
    registry: AdapterRegistry | None = None,
    orchestrator: Orchestrator | None = None,
) -> None:
    """Create and store core singletons on ``app.state``.

    All parameters are optional. When omitted, sensible defaults are
    created from the project settings (``config/settings.yaml`` + env).

    Tests should pass mock instances explicitly.
    """

    # --- Settings ---
    settings = get_settings()
    app.state.settings = settings

    # --- Message Bus ---
    if message_bus is None:
        message_bus = MessageBus()
    app.state.message_bus = message_bus

    # --- Session Manager ---
    if session_manager is None:
        # In-memory for now; swap to SQLite-backed via settings.storage.type
        session_manager = SessionManager(repository=None)
    app.state.session_manager = session_manager

    # --- Adapter Registry ---
    if registry is None:
        registry = AdapterRegistry()
        await _register_adapters(registry, settings)
    app.state.registry = registry

    # --- Orchestrator ---
    if orchestrator is None:
        orch_settings = settings.orchestrator
        orchestrator = Orchestrator(
            registry=registry,
            message_bus=message_bus,
            task_parser=TaskParser(llm_decompose=None),
            default_timeout=orch_settings.default_timeout,
            default_retries=orch_settings.default_retries,
            fallback_agent=orch_settings.fallback_agent,
        )
    app.state.orchestrator = orchestrator

    agents = await registry.list_agents()
    logger.info("Dependencies wired — agents: %s", agents)


# ---------------------------------------------------------------------------
# Internal: register adapters from settings
# ---------------------------------------------------------------------------


async def _register_adapters(registry: AdapterRegistry, settings) -> None:
    """Register adapters based on settings, gracefully handling missing CLIs."""
    agents_cfg = settings.agents

    # Claude
    if agents_cfg.claude.enabled:
        try:
            claude = ClaudeCodeAdapter(
                cli_path=agents_cfg.claude.cli_path,
                model=settings.llm.model,
                timeout=settings.orchestrator.default_timeout,
                api_key=settings.llm.api_key,
            )
            await registry.register(claude)
            logger.info("Registered adapter: claude")
        except Exception as exc:
            logger.warning("Failed to register Claude adapter: %s", exc)

    # Codex
    if agents_cfg.codex.enabled:
        try:
            codex = CodexCLIAdapter(
                cli_path=agents_cfg.codex.cli_path,
                timeout=settings.orchestrator.default_timeout,
            )
            await registry.register(codex)
            logger.info("Registered adapter: codex")
        except Exception as exc:
            logger.warning("Failed to register Codex adapter: %s", exc)
