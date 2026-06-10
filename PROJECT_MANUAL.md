# AgentHub — 项目手册 & 面试指南

> **一句话定义**: AgentHub 是一个多 AI Agent 协作平台，提供 IM 聊天风格的界面，
> 让用户通过自然语言并行调度多个 AI Agent（Claude Code、Codex CLI 等）协同完成任务。

---

## 目录

- [一、快速使用手册](#一快速使用手册)
- [二、项目全景架构](#二项目全景架构)
- [三、技术栈 & 关键指标](#三技术栈--关键指标)
- [四、核心模块深度解析](#四核心模块深度解析)
- [五、设计决策 & 为什么这么做](#五设计决策--为什么这么做)
- [六、踩坑记录 & Bug 库](#六踩坑记录--bug-库)
- [七、面试常见问题](#七面试常见问题)

---

## 一、快速使用手册

### 1.1 启动方式

```bash
# 终端 1：后端
uvicorn src.api.app:create_app --factory --reload --port 8000

# 终端 2：前端
streamlit run src/ui/app.py
# → 浏览器打开 http://localhost:8501
```

### 1.2 基本使用流程

```
1. 左侧边栏点击 "+ New Chat" 创建新会话
2. 输入框上方 multiselect 选择要调用的 Agent（如 claude）
3. 输入任务描述，支持多任务：
   - 编号列表: "1. fix login bug\n2. write tests\n3. review code"
   - 逗号分隔: "add login page, add dashboard, write tests"
   - @-mention: "@claude fix the bug @codex deploy"
4. 点击 "🚀 Send"
5. 实时观看 Agent 逐个返回结果（SSE 流式渲染）
```

### 1.3 API 端点速查

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/health` | 健康检查，返回 uptime + 在线 Agent 数 |
| `GET` | `/api/sessions/` | 列出活跃会话 |
| `POST` | `/api/sessions/` | 创建会话 `{title, session_type, participants}` |
| `GET` | `/api/sessions/{id}` | 获取单个会话详情 |
| `PATCH` | `/api/sessions/{id}/archive` | 归档会话 |
| `DELETE` | `/api/sessions/{id}` | 删除会话（软删除） |
| `POST` | `/api/chat/` | 发送消息 `{session_id, content}` → 返回 `task_id` |
| `GET` | `/api/chat/tasks/{task_id}` | 轮询任务状态 |
| `GET` | `/api/chat/tasks/{task_id}/stream` | SSE 实时流（`text/event-stream`） |

---

## 二、项目全景架构

### 2.1 分层架构图

```
┌─────────────────────────────────────────────────────┐
│                    Streamlit UI                     │
│  src/ui/app.py  +  src/ui/api_client.py            │
│  ┌──────────┐  ┌──────────────────────────────────┐│
│  │ Sidebar  │  │  Chat View                       ││
│  │ Sessions │  │  ┌───┐ ┌───┐ ┌───┐              ││
│  │  ├ New   │  │  │ U │ │ A │ │ S │  ...         ││
│  │  ├ Chat1 │  │  └───┘ └───┘ └───┘              ││
│  │  └ Chat2 │  │  [Agent Selector] [Input] [Send] ││
│  └──────────┘  └──────────────────────────────────┘│
│  SSE Thread: _consume_sse_stream() daemon          │
└──────────────────────┬──────────────────────────────┘
                       │ HTTP + SSE
┌──────────────────────▼──────────────────────────────┐
│                  FastAPI Layer                       │
│  src/api/app.py  (create_app factory)               │
│  ├─ CORS Middleware (allow localhost:8501)          │
│  ├─ RequestIDMiddleware (X-Request-ID header)       │
│  ├─ RequestLoggingMiddleware (structured access log)│
│  ├─ Exception Handlers (404/422/500 → JSON)         │
│  │                                                   │
│  ├─ /health              → HealthCheck              │
│  ├─ /api/sessions/*      → SessionManager           │
│  └─ /api/chat/*          → Orchestrator             │
│       ├─ POST /          → run_async() (non-blocking)│
│       ├─ GET /tasks/{id} → task_store lookup        │
│       └─ GET /tasks/{id}/stream → SSE replay + live │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│                Orchestrator Layer                    │
│  src/orchestrator/orchestrator.py                   │
│  ┌─────────────────────────────────────────────┐   │
│  │ run():       同步模式（阻塞到全部完成）     │   │
│  │ run_async(): 异步模式（立即返回 + 后台执行）│   │
│  └─────────────────────────────────────────────┘   │
│       │                                              │
│  ┌────▼────┐  ┌──────────┐  ┌───────────────┐      │
│  │Parser   │  │  Router  │  │   Executor    │      │
│  │@mention │→ │ keyword  │→ │ retry/fallback│      │
│  │decompose│  │ matching │  │ /timeout       │      │
│  └─────────┘  └──────────┘  └───────┬───────┘      │
│                                      │               │
│                    MessageBus ◄──────┘               │
│                    (progress events)                 │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│                 Adapter Layer                        │
│  src/adapters/                                       │
│  ┌─────────────────────┐  ┌─────────────────────┐   │
│  │ ClaudeCodeAdapter   │  │ CodexCLIAdapter     │   │
│  │ asyncio subprocess  │  │ asyncio subprocess  │   │
│  │ claude -p / --print │  │ codex exec / --stream│  │
│  └─────────────────────┘  └─────────────────────┘   │
│                ▲                    ▲                │
│                └── AbstractAgentAdapter ──┘          │
│                                                      │
│  src/adapters/registry.py                            │
│  AdapterRegistry: register / get / find_by_action    │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│                  Core Layer                          │
│  src/core/                                           │
│  ├── schema.py     ← 20 Pydantic V2 models          │
│  ├── config.py     ← pydantic-settings + YAML       │
│  ├── session.py    ← Session CRUD + invariants      │
│  └── message_bus.py ← async pub/sub per session     │
│                                                      │
│  src/repository/                                     │
│  ├── base.py          ← MessageRepository ABC       │
│  └── sqlite_repository.py ← aiosqlite impl          │
│                                                      │
│  src/tools/                                          │
│  ├── diff_viewer.py  ← unified diff + lang detect   │
│  ├── preview.py      ← background HTTP server       │
│  └── deploy.py       ← docker compose wrapper       │
└─────────────────────────────────────────────────────┘
```

### 2.2 项目目录结构

```
AgentHub/
├── config/
│   └── settings.yaml          # 全局配置（LLM、Agent、存储、端口）
├── docker/
│   ├── Dockerfile.backend     # Python 3.11-slim + FastAPI
│   ├── Dockerfile.frontend    # Python 3.11-slim + Streamlit
│   └── docker-compose.yml     # 双服务编排 + 内部网络
├── src/
│   ├── core/
│   │   ├── schema.py          # 20个Pydantic V2模型（所有层共享）
│   │   ├── config.py          # pydantic-settings + YAML加载
│   │   ├── session.py         # 会话管理器（CRUD + 不变量校验）
│   │   └── message_bus.py     # 异步pub/sub消息总线
│   │
│   ├── adapters/
│   │   ├── base.py            # AbstractAgentAdapter ABC
│   │   ├── claude_adapter.py  # claude CLI子进程包装
│   │   ├── codex_adapter.py   # codex CLI子进程包装
│   │   └── registry.py        # AdapterRegistry（注册/查找/健康检查）
│   │
│   ├── orchestrator/
│   │   ├── orchestrator.py    # 主编排器（run + run_async）
│   │   ├── agent_router.py    # 关键词→Action→Agent匹配+重试+降级
│   │   └── task_parser.py     # @mention提取+任务分解
│   │
│   ├── repository/
│   │   ├── base.py            # MessageRepository + SessionRepository ABC
│   │   └── sqlite_repository.py  # aiosqlite实现
│   │
│   ├── tools/
│   │   ├── diff_viewer.py     # 代码diff + 语言自动检测
│   │   ├── preview.py         # 静态文件预览服务器
│   │   └── deploy.py          # docker compose部署包装
│   │
│   ├── api/
│   │   ├── app.py             # FastAPI工厂（lifespan + CORS + 中间件）
│   │   ├── middleware.py      # RequestID / 日志 / 异常处理器
│   │   ├── dependencies.py    # 依赖注入（初始化并注入app.state）
│   │   └── routes/
│   │       ├── schemas.py     # API请求/响应Pydantic模型
│   │       ├── sessions.py    # 会话CRUD端点
│   │       └── chat.py        # 发送消息 + 轮询 + SSE流
│   │
│   └── ui/
│       ├── app.py             # Streamlit主应用（SSE流式渲染）
│       └── api_client.py      # 同步HTTP客户端
│
├── tests/
│   ├── test_schema.py         # 29 tests
│   ├── test_config.py         # 15 tests
│   ├── test_session.py        # 22 tests
│   ├── test_message_bus.py    # 14 tests
│   ├── test_adapters.py       # 48 tests
│   ├── test_orchestrator.py   # 57 tests
│   ├── test_repository.py     # 19 tests
│   ├── test_tools.py          # 28 tests
│   ├── test_api.py            # 38 tests
│   └── test_ui_client.py      # 16 tests
│   ── 总计 293 tests ──
│
├── .github/workflows/
│   └── ci.yml                 # 4-job CI: test + lint + docker + integration
├── CLAUDE.md                  # Vibecoding开发规范
├── pyproject.toml             # 项目配置 + 依赖 + pytest/ruff/mypy设置
└── .env.example               # 环境变量模板
```

### 2.3 数据流（一个请求的完整旅程）

```
用户输入: "@claude fix the login bug, write tests"
    │
    ▼
Streamlit UI (src/ui/app.py)
    │ POST /api/chat {session_id, content}
    ▼
FastAPI chat.py → Orchestrator.run_async()
    │
    ├─ 1. TaskParser.parse("@claude fix the login bug, write tests")
    │     ├─ extract_mentions() → ["claude"]
    │     ├─ strip_mentions()  → "fix the login bug, write tests"
    │     └─ decompose_comma() → ["fix the login bug", "write tests"]
    │     → 2 SubTask objects
    │
    ├─ 2. AgentRouter.assign(sub_tasks)
    │     ├─ _infer_action("fix the login bug") → "debugging"
    │     ├─ registry.find_by_action("debugging") → ["claude"]
    │     └─ st.assigned_agent = "claude"
    │
    ├─ 3. task_store[task_id] = task
    │     asyncio.create_task(_execute_background())
    │     201 {task_id, status:"running"}
    │
    ▼  (后台)
    _execute_background():
        for each sub_task:
            ├─ adapter.send_message(desc, context)
            │     └─ ClaudeCodeAdapter → subprocess "claude -p 'prompt'"
            ├─ on_progress_callback(st, result)
            │     ├─ MessageBus.publish("task:<id>", progress_event)
            │     └─ task_store[task_id] updated
        ├─ aggregate results → final_result
        └─ MessageBus.publish("task:<id>", complete_event)
```

---

## 三、技术栈 & 关键指标

### 3.1 技术栈

| 层 | 技术 | 说明 |
|------|------|------|
| **数据模型** | Pydantic V2 | 20个模型，全JSON序列化，杜绝V1 `class Config` |
| **配置** | pydantic-settings + YAML | `AGENTHUB_*` 环境变量覆盖，嵌套 key 用 `__` |
| **API 框架** | FastAPI + Uvicorn | lifespan 模式，CORS，SSE 流式 |
| **实时通信** | SSE (Server-Sent Events) | 二阶段：replay 已完成事件 + live MessageBus订阅 |
| **消息总线** | 自研 async pub/sub | asyncio.Queue per session，无外部依赖 |
| **Agent 集成** | asyncio subprocess | 包装 CLI 工具（claude --print, codex exec --stream） |
| **存储** | aiosqlite（默认）| Repository 模式，可换 PostgreSQL |
| **前端** | Streamlit | session_state 状态管理，daemon 线程 SSE 消费 |
| **容器化** | Docker ×2 + Compose | 前后端分离，内部网络 `agenthub-net` |
| **CI/CD** | GitHub Actions | pytest + ruff + docker build + integration smoke test |

### 3.2 关键指标

| 指标 | 数值 |
|------|------|
| 源代码文件 | 34 |
| 总代码行数 | ~8,200 |
| 类数量 | 67 |
| 函数/方法数量 | 192 |
| Pydantic 模型 | 20 |
| API 端点 | 8 (health + 4 sessions + 3 chat) |
| 测试数量 | 293 |
| 代码覆盖率 | 高（所有核心路径 + 降级路径 + 边界情况） |
| Python 版本 | 3.11+ |
| 包依赖数量 | 12 (生产) + 5 (开发) |

---

## 四、核心模块深度解析

### 4.1 Schema 层（`src/core/schema.py`）

**为什么放在第一位？** Schema 是所有模块的共享契约。Agent 之间、前后端之间、存储层都依赖同一套模型，先定义 Schema 避免后续接口不一致。

**关键设计点：**

```python
# 溯源设计 — 每个发现都能追溯到证据
class Evidence(BaseModel):
    source: str       # URL 或 Agent 名称
    excerpt: str      # 引用原文
    timestamp: datetime

class AnnotatedFinding(BaseModel):
    text: str
    evidence: list[Evidence]  # 可选证据链

# 枚举类 — str, Enum 确保 JSON 序列化安全
class MessageRole(str, Enum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"

# 编排任务 — 内置鲁棒性字段
class OrchestrationTask(BaseModel):
    retries: int = 3          # 子任务最大重试次数
    timeout: float = 60.0     # 子任务超时秒数
    fallback_agent: str | None = None  # 降级 Agent
```

**面试重点**：为什么 URL 用 `str` 而不是 `HttpUrl`？因为 `pydantic_core.Url` 对象无法被 LangGraph/Jinja2 序列化，会导致 `TypeError`。这是踩过的坑。

### 4.2 配置层（`src/core/config.py`）

**pydantic-settings 的嵌套覆盖机制：**

```yaml
# config/settings.yaml
orchestrator:
  default_retries: 3
  default_timeout: 60.0
```

```bash
# 环境变量覆盖（嵌套用 __）
export AGENTHUB_ORCHESTRATOR__DEFAULT_RETRIES=5
```

优先级：**env var > YAML > 代码默认值**。

**面试重点**：为什么不用 `os.environ.get()` 直接读？pydantic-settings 提供类型强制转换（`"5"` → `int` 5）、验证、嵌套模型支持，比手写 `os.environ` 更安全。

### 4.3 消息总线（`src/core/message_bus.py`）

**为什么自研而不是用 Redis/Kafka？**

版本 1.0 的约束：所有 Agent 和 UI 在同一进程中，跨进程通信不是当前瓶颈。自研 150 行的 `asyncio.Queue` 实现即可满需需求，且零运维成本。

**核心实现：**

```python
class MessageBus:
    def __init__(self):
        self._queues: dict[str, asyncio.Queue] = {}   # session_id → Queue
        self._subscribers: dict[str, list[Subscription]] = defaultdict(list)

    async def subscribe(self, session_id) -> Subscription:
        # 每个 session 一个 Queue，订阅者共享
        # Subscription 是 AsyncIterator，支持 async for

    async def publish(self, message: ChatMessage) -> int:
        # 推送到对应 session 的所有订阅者
        # 无订阅者时返回 0（不报错，不缓存）
```

**面试重点**：这是 Pub/Sub 模式的标准实现。核心 trade-off：无消息持久化（重启丢失），但换来零延迟和零外部依赖。

### 4.4 Adapter 层（`src/adapters/`）

**设计模式：策略模式 + 注册表模式**

```python
class AbstractAgentAdapter(ABC):
    @abstractmethod
    async def send_message(self, msg, context) -> AgentResponse: ...
    @abstractmethod
    async def stream_response(self, msg, context) -> AsyncIterator[str]: ...
    @abstractmethod
    def get_capabilities(self) -> AgentCapability: ...
    @abstractmethod
    async def health_check(self) -> AgentStatus: ...
```

**ClaudeCodeAdapter 实现要点：**

```python
# 非流式：claude -p "prompt"
# 流式：claude --print --output-format stream-json "prompt"
# 都通过 asyncio.create_subprocess_exec() 调用
# 超时通过 asyncio.wait_for() 控制
# 取消通过 proc.kill()
```

**AdapterRegistry 核心能力：**

```python
async def find_by_action(self, action: str) -> list[str]:
    # "code_generation" → ["claude", "codex"]
    # 用于 AgentRouter 的任务分配
```

**面试重点**：为什么用 subprocess 而不是 SDK？CLI 工具是用户已经配置好的环境，不需要额外认证配置。SDK 调用需要 API key 管理，CLI 已经解决了这个问题。

### 4.5 Orchestrator 层（`src/orchestrator/`）

**这是系统的核心大脑。**

#### TaskParser：任务解析

```python
# 策略链：numbered list → comma split → LLM decompose → fallback
async def parse(self, text) -> list[SubTask]:
    clean = strip_mentions(text)
    # 1. 编号列表: "1. fix bug\n2. write test" → 2个SubTask
    # 2. 逗号分隔: "fix bug, add feature" → 2个SubTask
    # 3. LLM分解（可选）: "build the whole app" → N个SubTask
    # 4. 单任务兜底
```

**面试重点**：为什么不用 LLM 分解所有任务？① 正则更快（<1ms vs 2-10s）；② LLM 可能返回不可控格式；③ 多数用户输入已经是结构化的（编号列表/逗号分隔）。

#### AgentRouter：带重试和降级的任务执行

```python
async def execute(self, task, on_progress=None) -> list[RoutingResult]:
    for sub_task in task.sub_tasks:
        for attempt in range(1, retries + 1):
            try:
                response = await adapter.send_message(...)
                if response.finish_reason == "stop":
                    break  # 成功
            except (TimeoutError, AgentAdapterError):
                continue  # 重试
        else:
            # 所有重试耗尽 → 尝试 fallback_agent
            response = await fallback_adapter.send_message(...)

        if on_progress:
            await on_progress(sub_task, result)  # SSE 回调
```

**面试重点**：retry + fallback 两级容错。为什么 fallback 是必要的？单一 Agent 可能因 API quota、网络问题、CLI bug 等原因不可用，fallback 保证了服务可用性。

#### Orchestrator：同步 vs 异步执行

```python
# 同步模式（用于 CLI 工具和测试）
async def run(self, ...) -> OrchestrationTask:
    # 阻塞直到所有子任务完成

# 异步模式（用于 API 层）
async def run_async(self, ..., task_store=None) -> OrchestrationTask:
    # 解析 + 分配（同步，快速）
    # asyncio.create_task(_execute_background())  ← 后台执行
    # 201 立即返回 {task_id, status:"running"}
```

**面试重点**：为什么提供两种模式？同步模式适合 CLI 和测试（简单直接），异步模式适合 Web API（不阻塞 HTTP 连接）。两者共享相同的解析和分配逻辑。

### 4.6 API 层（`src/api/`）

**中间件栈（请求 → 响应）：**

```
CORS → RequestID → RequestLogging → Route Handler → Exception Handlers
```

**异常处理四层体系：**

```python
# 1. Starlette HTTPException → JSON {success, error, request_id}
# 2. FastAPI RequestValidationError → 422 + field-level errors
# 3. Pydantic ValidationError → 同上
# 4. Exception catch-all → 500 (detail only in DEBUG)
```

**面试重点**：`RequestValidationError` 不是 `pydantic.ValidationError` 的子类（FastAPI 重新包装了），需要用 `hasattr(exc, "errors")` duck-typing 而非 `isinstance`。这是实际踩过的坑。

**SSE 流式设计（二阶段）：**

```
Phase 1 — Replay:  已完成的子任务立刻推送 progress 事件
Phase 2 — Live:    订阅 MessageBus("task:<id>")，实时推送新事件
                   收到 complete 事件后自动关闭
```

**面试重点**：为什么需要 Replay 阶段？后台执行可能在 SSE 连接建立前就完成了部分/全部子任务。Replay 确保客户端不会遗漏事件。

### 4.7 UI 层（`src/ui/`）

**Streamlit 的 SSR（Server-Side Rendering）模型：**

```
每个用户交互 → 整个脚本从顶到下重新执行 → 渲染新 UI
```

**SSE 流式渲染的挑战：**

Streamlit 不支持原生 WebSocket/SSE。解决方案：
1. **后台 daemon 线程**：消费 SSE，写入 `st.session_state.stream_buffer`
2. **增量索引渲染**：`stream_idx` 追踪已渲染事件数，每次只处理新事件
3. **自动轮询**：`is_streaming=True` 时，`time.sleep(0.4)` + `st.rerun()` 循环

```python
# 线程安全：每个线程独立 httpx.Client
def _consume_sse_stream(session_id, task_id, api_base):
    client = AgentHubClient(base_url=api_base)  # 独立实例
    for event in client.stream_task(task_id):
        st.session_state.stream_buffer[session_id].append(event)
        if event.get("event") == "complete":
            break
    st.session_state.is_streaming = False
```

**面试重点**：为什么不用 `st.experimental_fragment` 或 WebSocket？Streamlit 的设计哲学是"简单的 Python 脚本"，我们选择遵循其范式而非对抗它。后台线程 + 自动 rerun 是社区验证过的最稳定方案。

---

## 五、设计决策 & 为什么这么做

### 5.1 Pydantic V2 而非 V1

| V1 (`class Config`) | V2 (`model_config`) |
|---------------------|---------------------|
| `json_encoders` 无效 | `model_config = ConfigDict(...)` |
| `orm_mode` | `from_attributes = True` |
| 性能较慢 | Rust 核心，快 5-50 倍 |

### 5.2 Enum 用 `(str, Enum)` 而非 `Enum`

```python
class MessageRole(str, Enum):  # 正确
    USER = "user"  # json.dumps() → '"user"'

class MessageRole(Enum):       # 错误
    USER = "user"  # json.dumps() → 'MessageRole.USER'（无法序列化）
```

### 5.3 同步 HTTP 客户端（Streamlit）

Streamlit 的执行模型是同步的（每次交互重新运行脚本），`asyncio` 事件循环在 Streamlit 中不稳定。选择 `httpx.Client`（同步），SSE 消费放在 daemon 线程中。

### 5.4 前后端分离 + Docker 双容器

| 为什么 | 说明 |
|------|------|
| 独立扩缩容 | 前端无状态，后端可按 Agent 负载扩容 |
| 独立部署 | 修改 UI 不需要重启后端 |
| 网络隔离 | 前端通过 `http://backend:8000` 内部网络调用，不暴露后端端口 |
| 开发体验 | 本地开发可只启动后端（用 Swagger 调试）或全栈启动 |

### 5.5 为什么用 Streamlit 而不是 React/Vue？

1. **工期约束**：本项目 1 人 2 天完成全栈，React 前端需要额外 3-5 天
2. **目标用户**：开发者工具，UI 简洁优先于动画效果
3. **Python 全栈**：前后端都是 Python，技术栈统一
4. **快速迭代**：修改 Python 代码即刷新页面，无需构建步骤

### 5.6 Repository 模式而非直接 SQL

```python
class MessageRepository(ABC):
    @abstractmethod
    async def save(self, message): ...
    @abstractmethod
    async def get_by_session(self, session_id): ...

class SqliteMessageRepository(MessageRepository): ...
# 未来: PostgresMessageRepository(MessageRepository): ...
```

**好处**：① 单元测试用 Memory 实现（无需真实 DB）；② 存储后端可替换；③ SessionManager 不依赖具体存储实现。

---

## 六、踩坑记录 & Bug 库

### 6.1 Pydantic 相关

| Bug | 根因 | 修复 |
|------|------|------|
| `HttpUrl` 序列化变成 `pydantic_core.Url` 对象 | Pydantic V2 类型保留 | URL 字段全部用 `str` |
| `RequestValidationError` 不是 `ValidationError` 子类 | FastAPI 重新包装了 | 用 `hasattr(exc, "errors")` duck-typing |
| `class Config` 不生效 | V2 废弃了 | 全部迁移到 `model_config` |

### 6.2 字符串陷阱

| Bug | 根因 | 修复 |
|------|------|------|
| 输入"飞书"产出 6 个竞品(n,o,t,i,o,n) | `for target in "飞书"` 迭代字符串 | 入口处 `isinstance(targets, str)` → 包装为 `[targets]` |
| 中文逗号不识别 | `split(",")` 不匹配 `，` | `text.replace("，", ",").split(",")` |
| `list(products)` 迭代字符串 | `list("飞书")` → `['飞','书']` | `products if isinstance(products, list) else [products]` |

### 6.3 异步相关

| Bug | 根因 | 修复 |
|------|------|------|
| ASGITransport 不触发 lifespan | httpx 测试客户端不支持 FastAPI 的 lifespan 事件 | 在 `app.state` 设置同步默认值 |
| SSE 流挂起不关闭 | 不存在的 task_id 进入 live 订阅永不等 complete | 先检查 task_store，不存在直接返回 `complete(not_found)` |
| `await` 在 sync 函数中 | `setup_dependencies` 调 `await register()` 但自身不是 `async` | 改为 `async def` |

### 6.4 Streamlit 相关

| Bug | 根因 | 修复 |
|------|------|------|
| 页面加载 10+ 秒 | Google Fonts `@import` 超时 | 系统字体栈，零外部请求 |
| SSE 事件丢失 | 后台线程完成时主循环还没读到 buffer | `stream_idx` 索引追踪，只增不减 |
| 发送按钮在流式中可点击 | 未禁用 | `disabled=st.session_state.is_streaming` |

---

## 七、面试常见问题

### Q1: 这个系统的核心价值是什么？

**A**: 传统 AI 编程助手是 1v1 对话（你 ↔ 一个 Agent）。AgentHub 实现了 **1 对 N 的并行 Agent 协作**——用户描述一个复杂任务，系统自动拆解成子任务，路由给最合适的 Agent 并行/顺序执行，并实时流式返回结果。类比：从"雇一个程序员"升级到"管理一个 AI 开发团队"。

### Q2: 架构中最大的技术挑战是什么？

**A**: **实时流式反馈的异步编排**。三个难点：
1. **后台执行不阻塞 HTTP 连接**：`run_async()` 用 `asyncio.create_task()` 把执行丢到后台，POST 立即返回
2. **SSE 不丢失已完成事件**：二阶段设计（replay + live），先回放已完成的子任务，再订阅实时事件
3. **Streamlit 不支持 WebSocket/SSE**：daemon 线程 + `session_state` + 自动 rerun 实现"伪实时"

### Q3: AgentRouter 的 keyword matching 为什么不直接用 LLM？

**A**: 三层原因：
1. **延迟**：关键词匹配 <1ms，LLM 调用 2-10s
2. **可靠性**：LLM 可能返回不可控格式或幻觉
3. **确定性**：相同输入总是路由到相同 Agent，方便调试

当前 20 个关键词覆盖 80% 场景，LLM 路由作为下一步增强（已预留 `llm_decompose` 参数）。

### Q4: 如何处理 Agent 执行失败？

**A**: 三级容错机制：
1. **Retry**：子任务失败自动重试（默认 3 次）
2. **Fallback**：主 Agent 重试耗尽后切换到 `fallback_agent`（如 claude → codex）
3. **Graceful degradation**：部分子任务失败不影响其他子任务，结果中标记 FAILED

### Q5: MessageBus 为什么不直接用 Redis Pub/Sub？

**A**: 当前阶段所有组件在同一进程中，`asyncio.Queue` 的 150 行实现满足需求（零延迟、零运维）。架构预留了 `MessageBus` 的可替换接口，未来如果 Agent 进程化/容器化，只需实现一个新的 `RemoteMessageBus` 对接 Redis/Kafka，不改业务代码。

### Q6: 为什么 Pydantic URL 类型不用 `HttpUrl` 而用 `str`？

**A**: `pydantic.HttpUrl` 在序列化后变成 `pydantic_core.Url` 对象（不是字符串），传给 `json.dumps()`、Jinja2 模板渲染、LangGraph State 时会抛 `TypeError`。Pydantic V2 的类型保留特性对某些下游不兼容，所以统一用 `str`。

### Q7: 项目的测试策略是什么？

**A**: 
- **正常路径**：每个模块的所有核心功能有单元测试（293 tests）
- **降级路径**：Mock Agent 失败、超时、返回错误等场景（`test_retry_on_failure`, `test_timeout`, `test_total_failure`）
- **API 层**：422 验证错误、404 业务错误、CORS 头、Request ID 完整性
- **集成测试**：`test_ui_client.py` 启动真实 uvicorn server 进行端到端测试
- **CI**：GitHub Actions 在 py3.11/3.12 上跑 pytest + docker build + docker compose 冒烟测试

### Q8: 如果要扩展到 100 个 Agent，架构需要哪些变化？

**A**:
1. **进程化 Agent**：每个 Agent 独立进程/容器，通过 gRPC/消息队列通信
2. **Adapter 层升级**：从 subprocess 调用改为 HTTP/gRPC 远程调用
3. **MessageBus 升级**：从 `asyncio.Queue` 升级到 Redis/NATS/Kafka
4. **Orchestrator 并行执行**：当前顺序执行子任务，改为 `asyncio.gather()` 并行（已预留 `on_progress` 回调支持）
5. **连接池**：`AdapterRegistry` 增加连接池管理和负载均衡

### Q9: 为什么选择 Streamlit 而不是 React/Vue？

**A**:
- **工期**：Streamlit 2 天完成全功能 UI，React 需要 1-2 周
- **技术栈统一**：前后端都是 Python，降低维护成本
- **目标用户**：开发者工具不需要复杂的 CSS 动画或 SPA 路由
- **快速迭代**：`streamlit run --reload` 热更新，无需 webpack/vite 构建

### Q10: 如果前端有多个用户同时使用，Streamlit 能支持吗？

**A**: Streamlit 默认是单用户模型。多用户方案：
1. **Streamlit Community Cloud**（官方 SaaS）：自动管理 session
2. **每个用户独立容器**：Docker + Nginx 反向路由，每个 session 一个 Streamlit 实例
3. **迁移到 React**：当用户量增长到需要多租户时，Streamlit 可以用 `src/ui/api_client.py` 对接 React 前端（客户端已独立封装）

---
> 最后更新: 2026-06-10 | 测试数: 293 | 代码行数: ~8,200 | Python 3.11+
