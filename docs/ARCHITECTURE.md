# AgentHub Architecture Diagram

This diagram is rendered natively by GitHub. View it at:
https://github.com/quannie255-star/AgentHub/blob/main/docs/ARCHITECTURE.md

To export as PNG:
1. Open this file on GitHub
2. Right-click the diagram → Copy image
3. Or paste the Mermaid code into https://mermaid.live → Export PNG

---

## System Architecture (Data Flow)

```mermaid
graph TD
    subgraph FRONTEND["Frontend - Streamlit"]
        UI["Chat Interface<br/>IM-style UI"]
        CLIENT["AgentHubClient<br/>sync HTTP + SSE"]
        STHREAD["SSE Consumer Thread<br/>daemon<br/>stream_buffer → messages"]
    end

    subgraph API["API Layer - FastAPI"]
        direction TB
        MW1["CORS Middleware"]
        MW2["RequestID Middleware"]
        MW3["Logging Middleware"]
        EH["Exception Handlers<br/>404 JSON | 422 Validation | 500 Fallback"]
        R_HEALTH["/health"]
        R_SESSION["/api/sessions/*"]
        R_CHAT["/api/chat/*"]
    end

    subgraph ORCH["Orchestrator Layer"]
        PARSER["TaskParser<br/>@mention extract<br/>numbered/comma/LLM decompose"]
        ROUTER["AgentRouter<br/>keyword→action→agent<br/>retry 3x | fallback | timeout"]
        TSTORE["TaskStore<br/>dict[task_id]"]
    end

    subgraph ADAPTERS["Adapter Layer"]
        ABC["AbstractAgentAdapter"]
        CLAUDE["ClaudeCodeAdapter<br/>subprocess: claude CLI"]
        CODEX["CodexCLIAdapter<br/>subprocess: codex CLI"]
        REG["AdapterRegistry<br/>register | find_by_action | health"]
    end

    subgraph CORE["Core Layer"]
        SCHEMA["Schema<br/>20 Pydantic V2 models"]
        MB["MessageBus<br/>async pub/sub<br/>asyncio.Queue per session"]
        SM["SessionManager<br/>CRUD + invariants<br/>(group ≥ 2 participants)"]
        REPO["Repository<br/>SQLite (aiosqlite)<br/>+ swap to PostgreSQL"]
    end

    UI --> CLIENT
    CLIENT -->|"POST /api/chat"| R_CHAT
    STHREAD -->|"GET /tasks/{id}/stream"| R_CHAT

    R_CHAT --> PARSER
    R_SESSION --> SM
    R_HEALTH --> REG

    PARSER --> ROUTER
    ROUTER -->|"assign agents"| REG
    ROUTER -->|"execute"| CLAUDE
    ROUTER -->|"execute"| CODEX
    CLAUDE & CODEX -.->|"implement"| ABC

    ROUTER --> TSTORE
    ROUTER -->|"progress events"| MB
    MB -->|"subscribe(task:id)"| STHREAD

    SM --> REPO

    ALL --> SCHEMA
```

---

## Request Lifecycle (Sequence)

```mermaid
sequenceDiagram
    actor User
    participant UI as Streamlit UI
    participant API as FastAPI
    participant Orch as Orchestrator
    participant Router as AgentRouter
    participant Adapter as ClaudeAdapter
    participant Bus as MessageBus
    participant SSE as SSE Consumer

    User->>UI: "1. fix bug 2. write test 3. review"
    UI->>API: POST /api/chat {session_id, content}
    API->>Orch: run_async(session_id, content)

    Orch->>Orch: TaskParser.parse() → 3 SubTasks
    Orch->>Router: assign(sub_tasks)
    Router->>Router: keyword→action→agent matching
    Router-->>Orch: sub_tasks with assigned agents

    Orch->>API: 201 {task_id, status:"running"}
    API-->>UI: {task_id}
    UI->>SSE: start daemon thread
    SSE->>API: GET /tasks/{id}/stream

    Note over Orch: Background execution (asyncio.create_task)

    loop Each sub-task
        Orch->>Router: execute sub_task
        Router->>Adapter: send_message(desc, context)
        Adapter->>Adapter: subprocess: claude -p "..."
        Adapter-->>Router: AgentResponse
        Router->>Bus: publish("task:<id>", progress)
        Bus-->>SSE: event: progress
        SSE-->>UI: incremental render
    end

    Router->>Bus: publish("task:<id>", complete)
    Bus-->>SSE: event: complete
    SSE-->>UI: final render
```

---

## Component Dependency

```mermaid
graph LR
    subgraph "Layer 1: Schema"
        S["schema.py<br/>20 Pydantic models"]
    end

    subgraph "Layer 2: Core Services"
        CFG["config.py<br/>pydantic-settings"]
        SM2["session.py<br/>SessionManager"]
        MB2["message_bus.py<br/>MessageBus"]
        REPO2["repository/*<br/>SQLite"]
    end

    subgraph "Layer 3: Adapters"
        BASE["base.py<br/>AbstractAdapter ABC"]
        CLI["claude/codex<br/>CLI wrappers"]
        REG2["registry.py<br/>Registry"]
    end

    subgraph "Layer 4: Orchestration"
        TP["task_parser.py"]
        AR["agent_router.py"]
        ORCH2["orchestrator.py"]
    end

    subgraph "Layer 5: API"
        APP["app.py<br/>FastAPI factory"]
        MW_API["middleware.py"]
        ROUTES["routes/*<br/>chat, sessions"]
    end

    subgraph "Layer 6: UI"
        UI2["app.py<br/>Streamlit"]
        AC["api_client.py<br/>HTTP client"]
    end

    S --> CFG
    S --> SM2
    S --> MB2
    S --> REPO2
    S --> BASE
    CFG --> CLI
    BASE --> CLI
    S --> REG2
    BASE --> REG2
    S --> ORCH2
    REG2 --> ORCH2
    MB2 --> ORCH2
    S --> TP
    TP --> ORCH2
    S --> AR
    REG2 --> AR
    AR --> ORCH2
    S --> APP
    ORCH2 --> APP
    SM2 --> APP
    MB2 --> APP
    APP --> ROUTES
    S --> UI2
    UI2 --> AC
    AC --> APP
```

---

## Deployment Architecture

```mermaid
graph TD
    subgraph HOST["Docker Host"]
        subgraph NET["agenthub-net (bridge)"]
            BACKEND["Backend Container<br/>FastAPI :8000<br/>HEALTHCHECK /health"]
            FRONTEND2["Frontend Container<br/>Streamlit :8501<br/>HEALTHCHECK /_stcore/health"]
        end
        VOL["agenthub-data<br/>(SQLite persistence)"]
    end

    BROWSER["Browser :8501"] -->|"HTTP :8501"| FRONTEND2
    FRONTEND2 -->|"HTTP backend:8000"| BACKEND
    BACKEND --> VOL
    CURL["curl / GitHub Actions"] -->|"HTTP :8000"| BACKEND
```

---

> Diagrams rendered with Mermaid. View on GitHub for live rendering.
