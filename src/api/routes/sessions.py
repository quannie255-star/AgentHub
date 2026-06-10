"""Session routes — CRUD for chat sessions.

Wired to ``SessionManager`` via ``request.app.state.session_manager``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from src.api.routes.schemas import CreateSessionRequest, CreateSessionResponse, SessionSummary
from src.core.schema import APIResponse, SessionStatus, SessionType
from src.core.session import SessionNotFoundError, SessionError

router = APIRouter(prefix="/api/sessions", tags=["Sessions"])


# ---------------------------------------------------------------------------
# GET /api/sessions  — list active
# ---------------------------------------------------------------------------

@router.get("/", response_model=APIResponse)
async def list_sessions(request: Request, limit: int = 20, offset: int = 0) -> APIResponse:
    """List active sessions, most-recently-updated first."""
    mgr = request.app.state.session_manager
    sessions = await mgr.list_active(limit=limit, offset=offset)
    summaries = [
        SessionSummary(
            id=s.id,
            title=s.title,
            type=s.type.value,
            status=s.status.value,
            participants=s.participants,
            updated_at=s.updated_at.isoformat(),
        )
        for s in sessions
    ]
    return APIResponse(
        success=True,
        data=[s.model_dump() for s in summaries],
        request_id=getattr(request.state, "request_id", None),
    )


# ---------------------------------------------------------------------------
# GET /api/sessions/{session_id}  — get one
# ---------------------------------------------------------------------------

@router.get("/{session_id}", response_model=APIResponse)
async def get_session(session_id: str, request: Request) -> APIResponse:
    """Get a single session by ID."""
    mgr = request.app.state.session_manager
    try:
        s = await mgr.get(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return APIResponse(
        success=True,
        data=CreateSessionResponse(
            id=s.id,
            title=s.title,
            type=s.type.value,
            status=s.status.value,
            participants=s.participants,
            created_at=s.created_at.isoformat(),
            updated_at=s.updated_at.isoformat(),
        ).model_dump(),
        request_id=getattr(request.state, "request_id", None),
    )


# ---------------------------------------------------------------------------
# POST /api/sessions  — create
# ---------------------------------------------------------------------------

@router.post("/", status_code=201, response_model=APIResponse)
async def create_session(body: CreateSessionRequest, request: Request) -> APIResponse:
    """Create a new chat session.

    Returns 201 with the created session on success.
    Returns 400 if invariants are violated (e.g. group with 1 participant).
    """
    mgr = request.app.state.session_manager
    try:
        s = await mgr.create(
            title=body.title,
            session_type=SessionType(body.session_type),
            participants=body.participants,
        )
    except SessionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return APIResponse(
        success=True,
        data=CreateSessionResponse(
            id=s.id,
            title=s.title,
            type=s.type.value,
            status=s.status.value,
            participants=s.participants,
            created_at=s.created_at.isoformat(),
            updated_at=s.updated_at.isoformat(),
        ).model_dump(),
        request_id=getattr(request.state, "request_id", None),
    )


# ---------------------------------------------------------------------------
# DELETE /api/sessions/{session_id}  — delete
# ---------------------------------------------------------------------------

@router.delete("/{session_id}", response_model=APIResponse)
async def delete_session(session_id: str, request: Request) -> APIResponse:
    """Delete a session (soft-delete, sets status to 'deleted')."""
    mgr = request.app.state.session_manager
    try:
        await mgr.delete(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return APIResponse(
        success=True,
        data={"id": session_id, "status": "deleted"},
        request_id=getattr(request.state, "request_id", None),
    )


# ---------------------------------------------------------------------------
# PATCH /api/sessions/{session_id}/archive  — archive
# ---------------------------------------------------------------------------

@router.patch("/{session_id}/archive", response_model=APIResponse)
async def archive_session(session_id: str, request: Request) -> APIResponse:
    """Archive a session."""
    mgr = request.app.state.session_manager
    try:
        s = await mgr.archive(session_id)
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return APIResponse(
        success=True,
        data=CreateSessionResponse(
            id=s.id,
            title=s.title,
            type=s.type.value,
            status=s.status.value,
            participants=s.participants,
            created_at=s.created_at.isoformat(),
            updated_at=s.updated_at.isoformat(),
        ).model_dump(),
        request_id=getattr(request.state, "request_id", None),
    )
