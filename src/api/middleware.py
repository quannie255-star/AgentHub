"""FastAPI middleware — request ID, structured logging, and exception handling.

All middleware is added to the FastAPI app during ``create_app()``.
"""

from __future__ import annotations

import logging
import time
import traceback
import uuid

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

logger = logging.getLogger("agenthub.api")

# ---------------------------------------------------------------------------
# Request ID
# ---------------------------------------------------------------------------

REQUEST_ID_HEADER = "X-Request-ID"


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Ensure every request carries an ``X-Request-ID`` header.

    If the client sends one it is honoured; otherwise a short UUID is
    generated and attached.  The value is stored in ``request.state.request_id``
    and echoed back in the response headers.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


# ---------------------------------------------------------------------------
# Request logging
# ---------------------------------------------------------------------------

class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log every request with method, path, status, request_id, and latency.

    Log format (structured)::

        [request_id] method path → status (latency_ms ms)
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        rid = getattr(request.state, "request_id", "-")
        start = time.perf_counter()

        response = await call_next(request)

        latency_ms = (time.perf_counter() - start) * 1000
        self._log(rid, request.method, request.url.path, response.status_code, latency_ms)
        return response

    @staticmethod
    def _log(
        rid: str, method: str, path: str, status: int, latency_ms: float
    ) -> None:
        logger.info(
            "[%s] %s %s → %d (%.1f ms)",
            rid, method, path, status, latency_ms,
        )


# ---------------------------------------------------------------------------
# Exception → JSON error envelope
# ---------------------------------------------------------------------------

async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Fallback handler for unhandled Starlette / FastAPI HTTP exceptions."""
    from starlette.exceptions import HTTPException as StarletteHTTPException

    rid = getattr(request.state, "request_id", "-")

    if isinstance(exc, StarletteHTTPException):
        status_code = exc.status_code
        detail = exc.detail
    else:
        status_code = 500
        detail = "Internal server error"

    logger.error(
        "[%s] HTTP %d: %s\n%s",
        rid, status_code, detail,
        traceback.format_exc(),
    )
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "error": str(detail) if isinstance(detail, str) else "HTTP error",
            "detail": detail if isinstance(detail, str) else None,
            "request_id": rid,
        },
    )


async def _validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return 422 for Pydantic ``ValidationError`` / FastAPI ``RequestValidationError``.

    Both carry an ``.errors()`` method that returns a list of field-level
    error dicts (loc, msg, type).  FastAPI wraps Pydantic errors in
    ``RequestValidationError`` which is NOT a subclass of
    ``pydantic.ValidationError``, so we use duck-typing.
    """
    from pydantic import ValidationError

    rid = getattr(request.state, "request_id", "-")
    logger.warning("[%s] Validation error: %s", rid, exc)

    # Both pydantic.ValidationError and fastapi.exceptions.RequestValidationError
    # expose .errors() → list[dict]
    errors: list[dict] = (
        exc.errors() if hasattr(exc, "errors") else []
    )
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "error": "Validation error",
            "detail": errors,
            "request_id": rid,
        },
    )


async def _catch_all_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler for any unhandled exception."""
    rid = getattr(request.state, "request_id", "-")
    logger.exception("[%s] Unhandled exception: %s", rid, exc)

    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": "Internal server error",
            "detail": str(exc) if logger.isEnabledFor(logging.DEBUG) else None,
            "request_id": rid,
        },
    )


def register_exception_handlers(app: ASGIApp) -> None:
    """Register structured JSON exception handlers on the FastAPI app."""
    from fastapi.exceptions import RequestValidationError
    from pydantic import ValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException

    # Order matters: most specific first
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(ValidationError, _validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _catch_all_exception_handler)  # type: ignore[arg-type]
