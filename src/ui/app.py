"""AgentHub Streamlit UI — IM-style multi-agent chat interface.

Layout::

    ┌──────────┬──────────────────────────────────┐
    │ Sidebar  │  Chat Area                       │
    │          │  ┌────────────────────────────┐  │
    │ Sessions │  │  Message bubbles           │  │
    │  ├ New   │  │  (user / agent / system)   │  │
    │  ├ Chat1 │  └────────────────────────────┘  │
    │  └ Chat2 │  ┌────────────────────────────┐  │
    │          │  │ [Agent selector] [Input...]│  │
    │          │  │ [Send]                     │  │
    │          │  └────────────────────────────┘  │
    └──────────┴──────────────────────────────────┘

Session state structure (``st.session_state``)::

    {
        "api_client":       AgentHubClient,
        "current_session_id": str | None,
        "messages":         dict[str, list[dict]],   # session_id → messages
        "agents":           list[str],                # available agent names
        "is_streaming":     bool,
        "stream_task_id":   str | None,              # active SSE task
        "stream_buffer":    dict[str, list[dict]],   # session_id → pending events
        "backend_ok":       bool,
    }

Streaming flow:
    1. User sends message → POST /api/chat → get task_id
    2. Background thread connects to SSE endpoint
    3. Thread writes parsed events → stream_buffer[sid]
    4. Main loop reads stream_buffer, appends to messages, reruns
    5. On "complete" event, thread sets is_streaming = False

Usage::

    streamlit run src/ui/app.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

# Add project root to path so we can import from src/
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from src.ui.api_client import AgentHubAPIError, AgentHubClient

# ======================================================================
# System font stack — NO external font imports
# ======================================================================
_SYSTEM_FONT = (
    '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, '
    '"Helvetica Neue", Arial, "Noto Sans", sans-serif, '
    '"Apple Color Emoji", "Segoe UI Emoji"'
)

# ======================================================================
# Page config — must be first Streamlit call
# ======================================================================
st.set_page_config(
    page_title="AgentHub",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    f"""<style>
    html, body, [class*="css"] {{
        font-family: {_SYSTEM_FONT} !important;
    }}
    .stApp {{
        font-family: {_SYSTEM_FONT} !important;
    }}
    .streaming-indicator {{
        animation: pulse 1.5s ease-in-out infinite;
    }}
    @keyframes pulse {{
        0%, 100% {{ opacity: 1; }}
        50% {{ opacity: 0.5; }}
    }}
    </style>""",
    unsafe_allow_html=True,
)


# ======================================================================
# Session state initialisation
# ======================================================================
def _init_session_state() -> None:
    import os
    api_url = os.environ.get("AGENTHUB_API_URL", "http://localhost:8000")
    defaults = {
        "api_client": AgentHubClient(base_url=api_url),
        "current_session_id": None,
        "messages": {},          # session_id → list[dict]
        "agents": [],
        "is_streaming": False,
        "stream_task_id": None,
        "stream_buffer": {},     # session_id → list[dict] (all SSE events so far)
        "stream_idx": {},        # session_id → int (how many events already rendered)
        "stream_error": None,    # error message if SSE thread fails
        "backend_ok": False,
        "_health_checked": False,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


_init_session_state()

# ======================================================================
# Backend health check (once per session)
# ======================================================================
if not st.session_state._health_checked:
    try:
        health = st.session_state.api_client.health()
        st.session_state.backend_ok = health.get("status") == "ok"
        st.session_state.agents = st.session_state.api_client.list_agents()
    except AgentHubAPIError:
        st.session_state.backend_ok = False
        st.session_state.agents = []
    st.session_state._health_checked = True


# ======================================================================
# Streaming: background thread
# ======================================================================
def _consume_sse_stream(session_id: str, task_id: str, api_base: str) -> None:
    """Background thread target — consume SSE and populate stream_buffer.

    Runs in a daemon thread.  Writes parsed SSE events into
    ``st.session_state.stream_buffer[session_id]``.  The main Streamlit
    loop picks them up and appends them to ``messages``.
    """
    # Each thread gets its own client (httpx.Client is not thread-safe)
    client = AgentHubClient(base_url=api_base, timeout=120.0)
    buffer: list[dict] = []

    try:
        for event in client.stream_task(task_id):
            buffer.append(event)
            # Push to session_state so the main loop sees it
            st.session_state.stream_buffer[session_id] = list(buffer)

            if event.get("event") == "complete":
                break
    except AgentHubAPIError as e:
        st.session_state.stream_error = str(e)
    except Exception as e:
        st.session_state.stream_error = f"Stream error: {e}"
    finally:
        client.close()
        st.session_state.is_streaming = False
        st.session_state.stream_task_id = None


def _start_streaming(session_id: str, task_id: str) -> None:
    """Kick off a background SSE consumer thread."""
    st.session_state.is_streaming = True
    st.session_state.stream_task_id = task_id
    st.session_state.stream_buffer[session_id] = []
    st.session_state.stream_error = None

    # Reset render index for this new stream
    st.session_state.stream_idx[session_id] = 0

    thread = threading.Thread(
        target=_consume_sse_stream,
        args=(session_id, task_id, st.session_state.api_client._base),
        daemon=True,
    )
    thread.start()


# ======================================================================
# Helpers
# ======================================================================
def _refresh_sessions() -> list[dict]:
    try:
        sessions = st.session_state.api_client.list_sessions()
        return sorted(sessions, key=lambda s: s.get("updated_at", ""), reverse=True)
    except AgentHubAPIError:
        return []


# ======================================================================
# Sidebar — session list
# ======================================================================
with st.sidebar:
    st.title("🤖 AgentHub")

    if not st.session_state.backend_ok:
        st.error("⚠️ Backend unreachable")
        st.caption("Start with: `uvicorn src.api.app:create_app --factory`")
        st.stop()

    if st.button("➕ New Chat", use_container_width=True, disabled=st.session_state.is_streaming):
        try:
            new_s = st.session_state.api_client.create_session(title="New Chat")
            st.session_state.current_session_id = new_s["id"]
            st.session_state.messages[new_s["id"]] = []
            st.rerun()
        except AgentHubAPIError as e:
            st.error(str(e))

    st.divider()

    sessions = _refresh_sessions()
    for s in sessions:
        sid = s["id"]
        label = s.get("title", "Untitled")[:30]
        p_count = len(s.get("participants", []))
        if p_count:
            label += f"  ({p_count})"

        col1, col2 = st.columns([4, 1])
        with col1:
            is_current = (sid == st.session_state.current_session_id)
            btn_label = f"{'📌 ' if is_current else ''}{label}"
            if st.button(
                btn_label,
                key=f"sel_{sid}",
                use_container_width=True,
                type="primary" if is_current else "secondary",
            ):
                st.session_state.current_session_id = sid
                if sid not in st.session_state.messages:
                    st.session_state.messages[sid] = []
                st.rerun()
        with col2:
            if st.button("🗑️", key=f"del_{sid}", help="Delete session"):
                try:
                    st.session_state.api_client.delete_session(sid)
                    if st.session_state.current_session_id == sid:
                        st.session_state.current_session_id = None
                    st.session_state.messages.pop(sid, None)
                    st.session_state.stream_buffer.pop(sid, None)
                    st.rerun()
                except AgentHubAPIError as e:
                    st.error(str(e))

    st.divider()
    agents_str = ", ".join(st.session_state.agents) if st.session_state.agents else "none"
    st.caption(f"Agents: {agents_str}")
    st.caption(f"API: {st.session_state.api_client._base}")


# ======================================================================
# Main chat area
# ======================================================================
st.title("AgentHub Chat" if not st.session_state.current_session_id else "Chat")

if st.session_state.current_session_id is None:
    st.info("👈 Select a session from the sidebar or create a new one.")
    st.stop()

sid = st.session_state.current_session_id

# --- Drain stream buffer into messages (incremental, index-based) ---
buf = st.session_state.stream_buffer.get(sid, [])
idx = st.session_state.stream_idx.get(sid, 0)
new_events = buf[idx:]  # events we haven't rendered yet

for event in new_events:
    etype = event.get("event", "")
    if etype == "progress":
        agent = event.get("assigned_agent", "unknown")
        desc = event.get("description", "")
        result = event.get("result", "")
        status = event.get("status", "unknown")

        content = f"**{desc}**" + (f"\n\n{result}" if result else f"\n\n*{status}*")
        st.session_state.messages[sid].append({
            "role": "agent",
            "sender": agent,
            "content": content,
        })
    elif etype == "connected":
        pass  # internal event, not displayed
    elif etype == "complete":
        if event.get("status") == "failed" and event.get("final_result"):
            st.session_state.messages[sid].append({
                "role": "system",
                "sender": "orchestrator",
                "content": f"⚠️ {event['final_result']}",
            })

# Advance the index
st.session_state.stream_idx[sid] = len(buf)

# --- Render messages ---
messages = st.session_state.messages.get(sid, [])

chat_container = st.container()
with chat_container:
    if not messages:
        st.markdown("### ✨ Start the conversation")
        st.caption("Tip: Use @agent mentions to route tasks.")

    for msg in messages:
        role = msg.get("role", "user")
        sender = msg.get("sender", "unknown")
        content = msg.get("content", "")

        if role == "system":
            with st.expander(f"🔧 System — {sender}", expanded=False):
                st.markdown(content)
        elif role == "agent":
            with st.chat_message("assistant", avatar="🤖"):
                st.caption(f"**{sender}**")
                st.markdown(content, unsafe_allow_html=False)
        else:
            with st.chat_message("user", avatar="👤"):
                st.caption(f"**{sender}**")
                st.markdown(content, unsafe_allow_html=False)

# --- Streaming indicator ---
if st.session_state.is_streaming:
    stream_placeholder = st.empty()
    with stream_placeholder.container():
        col_a, col_b = st.columns([1, 20])
        with col_a:
            st.markdown("⏳")
        with col_b:
            st.caption("Agents working...")

    if st.session_state.stream_error:
        st.error(f"Stream error: {st.session_state.stream_error}")
        st.session_state.is_streaming = False

    # Auto-rerun to check for new streaming events
    time.sleep(0.4)
    st.rerun()


# ======================================================================
# Input area — Agent selector + text input + send
# ======================================================================
st.divider()

col1, col2 = st.columns([3, 1])
with col1:
    selected_agents = st.multiselect(
        "Target agents (optional)",
        options=st.session_state.agents,
        default=[],
        placeholder="Select agents to mention...",
        label_visibility="collapsed",
        key="agent_selector",
        disabled=st.session_state.is_streaming,
    )
    mention_prefix = " ".join(f"@{a}" for a in selected_agents) + (" " if selected_agents else "")

    user_input = st.text_area(
        "Message",
        placeholder="Describe your task... (e.g. fix the login bug)",
        label_visibility="collapsed",
        key="user_input",
        height=68,
        disabled=st.session_state.is_streaming,
    )

with col2:
    send_disabled = st.session_state.is_streaming or not user_input.strip()
    if st.button(
        "🚀 Send", use_container_width=True, disabled=send_disabled,
        type="primary",
    ):
        full_message = f"{mention_prefix}{user_input.strip()}"

        # Add user message
        st.session_state.messages[sid].append({
            "role": "user",
            "sender": "You",
            "content": full_message,
        })

        # Send to backend (async mode — returns task_id immediately)
        try:
            result = st.session_state.api_client.send_message(
                session_id=sid,
                content=full_message,
            )

            # Start SSE streaming in background thread
            _start_streaming(sid, result["task_id"])

        except AgentHubAPIError as e:
            st.session_state.messages[sid].append({
                "role": "system",
                "sender": "error",
                "content": f"❌ {e}",
            })

        st.session_state.user_input = ""
        st.rerun()
