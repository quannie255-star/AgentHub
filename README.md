# 🤖 AgentHub — Multi-Agent Collaboration Platform

[![CI](https://github.com/quannie255-star/AgentHub/actions/workflows/ci.yml/badge.svg)](https://github.com/quannie255-star/AgentHub/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688)](https://fastapi.tiangolo.com/)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.35%2B-FF4B4B)](https://streamlit.io/)
[![Tests](https://img.shields.io/badge/tests-293%20passed-brightgreen)](tests/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**AgentHub** is an IM-style multi-AI-agent collaboration platform. Users describe complex tasks in natural language, and the system automatically decomposes them into sub-tasks, routes each to the most capable AI agent (Claude Code, Codex CLI, or custom agents), and streams results back in real time.

> Think: Slack for AI agents — you manage a team of AI assistants from a single chat interface.

---

## Architecture

```mermaid
graph TB
    subgraph 用户层["👤 用户层"]
        UI["🖥️ Streamlit Chat UI<br/>多会话管理 · Agent多选 · SSE实时渲染"]
    end

    subgraph API层["🌐 API 层"]
        API["FastAPI + Uvicorn<br/>━━━━━━━━━━━━<br/>• /api/chat/ → 任务分发<br/>• /api/chat/tasks/{id}/stream → SSE推送<br/>• /api/sessions/ → 会话管理"]
    end

    subgraph 编排层["🎯 编排层"]
        TP["TaskParser<br/>━━━━━━━━━━━━<br/>• 编号列表解析<br/>• 逗号分隔解析<br/>• @-mention 提取"]
        AR["AgentRouter<br/>━━━━━━━━━━━━<br/>• 关键词匹配路由<br/>• LLM 语义路由<br/>• 3次重试 + fallback"]
        ORCH["Orchestrator<br/>协调调度"]
    end

    subgraph Agent层["🤖 Agent 适配层"]
        direction LR
        BASE["AbstractAgentAdapter<br/>抽象基类（3个方法）"]
        CLAUDE["ClaudeCodeAdapter<br/>asyncio subprocess"]
        CODEX["CodexCLIAdapter<br/>asyncio subprocess"]
        REGISTRY["AdapterRegistry<br/>插件注册中心"]
    end

    subgraph 基础设施层["⚙️ 基础设施"]
        direction LR
        MB["📨 MessageBus<br/>asyncio.Queue per session<br/>发布/订阅"]
        REPO["💾 Repository<br/>aiosqlite<br/>可替换存储"]
        TOOLS["🔧 工具集<br/>Diff Viewer · Preview<br/>Deploy"]
    end

    UI -->|"HTTP + SSE"| API
    API --> ORCH
    ORCH --> TP
    ORCH --> AR
    AR --> BASE
    BASE --> CLAUDE
    BASE --> CODEX
    REGISTRY --> BASE
    MB -.->|事件通知| API
    REPO -.->|持久化| API

    style 用户层 fill:#e1f5fe,stroke:#0288d1
    style API层 fill:#fff3e0,stroke:#f57c00
    style 编排层 fill:#e8f5e9,stroke:#388e3c
    style Agent层 fill:#f3e5f5,stroke:#7b1fa2
    style 基础设施层 fill:#fce4ec,stroke:#c62828
```

## Quick Start

```bash
# Terminal 1: Backend
uvicorn src.api.app:create_app --factory --reload --port 8000

# Terminal 2: Frontend
streamlit run src/ui/app.py
# → Open http://localhost:8501
```

## Features

- **Multi-Agent Routing**: Keyword-based task → agent matching (`fix` → debugging → Claude)
- **Task Decomposition**: Numbered lists, comma-separated, @-mentions
- **SSE Streaming**: Real-time progress via Server-Sent Events (two-phase: replay + live)
- **Retry & Fallback**: 3 retries per sub-task + configurable fallback agent
- **IM-Style UI**: Streamlit chat interface with multi-select agent picker
- **Docker Deploy**: Separate backend + frontend containers with health checks
- **CI/CD**: GitHub Actions — pytest, ruff lint, Docker build, integration smoke test

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Data Models | Pydantic V2 (20 models) |
| API Server | FastAPI + Uvicorn (lifespan pattern) |
| Real-time | SSE (Server-Sent Events) + custom async pub/sub MessageBus |
| Agent Integration | asyncio subprocess (CLI wrappers) |
| Storage | aiosqlite (Repository pattern, swappable) |
| Frontend | Streamlit (daemon thread SSE consumption) |
| Containerization | Docker ×2 + Compose |
| CI | GitHub Actions (4 jobs: test, lint, build, integration) |

## Project Structure

```
src/
├── core/           # Schema (20 Pydantic models), Config, Session, MessageBus
├── adapters/       # Abstract adapter ABC, Claude/Codex CLI wrappers, Registry
├── orchestrator/   # TaskParser, AgentRouter (retry/fallback), Orchestrator
├── repository/     # Abstract repos + SQLite implementation
├── tools/          # Diff viewer, Preview server, Deploy wrapper
├── api/            # FastAPI app, middleware (CORS/RequestID/logging), routes
└── ui/             # Streamlit app, sync HTTP client with SSE support
tests/              # 293 tests across all modules
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check (uptime, agent count) |
| `GET/POST` | `/api/sessions/` | List / Create sessions |
| `GET/DELETE` | `/api/sessions/{id}` | Get / Delete session |
| `PATCH` | `/api/sessions/{id}/archive` | Archive session |
| `POST` | `/api/chat/` | Send message → task_id (async, non-blocking) |
| `GET` | `/api/chat/tasks/{id}` | Poll task status |
| `GET` | `/api/chat/tasks/{id}/stream` | SSE streaming (text/event-stream) |

## Configuration

```bash
cp .env.example .env
# Edit: AGENTHUB_LLM_API_KEY=sk-...

# Or via environment:
export AGENTHUB_LLM__MODEL=claude-opus-4-8
export AGENTHUB_ORCHESTRATOR__DEFAULT_RETRIES=5
```

See `PROJECT_MANUAL.md` for deep-dive architecture docs and interview preparation guide.

## License

MIT
