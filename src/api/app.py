"""FastAPI application factory.

Creates the AgentHub API server with CORS, middleware, route
registration, and dependency injection.  Call ``create_app()`` to get a
fully-configured FastAPI instance ready for ``uvicorn.run()``.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from src.api.dependencies import setup_dependencies
from src.api.middleware import (
    RequestIDMiddleware,
    RequestLoggingMiddleware,
    register_exception_handlers,
)
from src.core.schema import HealthCheck


def create_app() -> FastAPI:
    """Build and return the AgentHub FastAPI application.

    Middleware stack (outer → inner):
      1. **CORS** — allows configured origins (Streamlit UI, dev tools).
      2. **Request ID** — attaches ``X-Request-ID`` to every request/response.
      3. **Request Logging** — structured access log.

    Exception handlers:
      - Starlette ``HTTPException`` → JSON ``{success, error, request_id}``
      - Pydantic ``ValidationError`` → 422 with field-level errors
      - Catch-all ``Exception`` → 500 (detail only in DEBUG)

    Dependencies are stored on ``app.state`` and wired into routes via
    ``request.app.state.xxx``.  See ``src/api/dependencies.py`` for the
    full list.
    """

    # ------------------------------------------------------------------
    # Lifespan — wire dependencies on startup
    # ------------------------------------------------------------------
    _started_at = time.time()

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # Startup
        app.state.started_at = _started_at
        app.state._deps_ready = False
        if not getattr(app.state, "_deps_ready", False):
            await setup_dependencies(app)
            app.state._deps_ready = True
        yield
        # Shutdown — nothing to clean up for now

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------
    app = FastAPI(
        title="AgentHub API",
        description="Multi-Agent Collaboration Platform — REST + SSE API",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=_lifespan,
    )

    # Set synchronous defaults so tests that don't trigger lifespan
    # (e.g. httpx.ASGITransport) still have a valid app.state.
    app.state.started_at = _started_at
    app.state._deps_ready = False
    app.state.task_store: dict[str, object] = {}

    # ------------------------------------------------------------------
    # CORS
    # ------------------------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:8501",   # Streamlit default
            "http://localhost:8080",   # alternative dev port
            "http://127.0.0.1:8501",
            "http://127.0.0.1:8080",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    # ------------------------------------------------------------------
    # Custom middleware (outer → inner: Logging wraps RequestID)
    # ------------------------------------------------------------------
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(RequestIDMiddleware)

    # ------------------------------------------------------------------
    # Exception handlers
    # ------------------------------------------------------------------
    register_exception_handlers(app)

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @app.get("/health", response_model=HealthCheck, tags=["System"])
    async def health(request: Request) -> HealthCheck:
        """Health-check endpoint.

        Returns uptime and agent count.  Always responds 200 when the
        process is alive; check ``agents_online`` for degradation.
        """
        uptime = time.time() - request.app.state.started_at
        agents_online = 0
        if getattr(request.app.state, "_deps_ready", False):
            try:
                results = await request.app.state.registry.health_check_all()
                agents_online = sum(
                    1 for s in results.values() if s.value == "idle"
                )
            except Exception:
                agents_online = -1  # registry not wired
        return HealthCheck(
            status="ok",
            version="0.1.0",
            uptime_seconds=round(uptime, 2),
            agents_online=agents_online,
        )

    # Lazy import routes to avoid circular deps
    from src.api.routes.sessions import router as sessions_router
    from src.api.routes.chat import router as chat_router

    app.include_router(sessions_router)
    app.include_router(chat_router)

    return app
