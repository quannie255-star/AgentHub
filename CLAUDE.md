# Vibecoding 实战手册

> 本手册记录了「AI 竞品分析 Agent 协作系统」项目从零到交付的完整流程、工程约束和踩坑经验。
> 新终端使用方式：将本文件放到新项目根目录，告诉 Claude Code "请先阅读 VIBECODING_PLAYBOOK.md，然后按照里面的流程开始工作"。

---

## 一、核心工作流：Plan-and-Execute

### 1.1 规则（不可违反）

```
1. 绝对禁止一次性生成所有代码
2. 每个需求必须先生成【技术方案】+【WBS 任务拆解清单】
3. 方案输出后必须停下来问"是否同意该计划？"
4. 等待用户明确说"同意，请执行第 X 步"再动手
5. 每次只执行一个最小可验证步骤
6. 每步完成后必须说明验证方法（用什么命令测试）
7. 报错时优先修复当前步骤，不追求进度
8. 每步开始前回顾上一步产出，确保接口对齐
```

### 1.2 标准对话模板

```
你：
  我有一个需求，想做 XX 系统，功能包括 A、B、C

Claude：
  ## 【技术方案】XX 系统
  ### 一、系统架构 (ASCII 图)
  ### 二、项目目录结构
  ### 三、WBS 任务拆解
  | 步骤 | 产出 | 输入 | 验证方法 |
  ### 四、依赖关系图
  是否同意该计划？

你：
  同意，请执行第 1 步

Claude：
  ## 🔙 回顾上一步 [如果有]
  [写代码，只写这一步的]
  ## 验证方法
  [告诉你跑什么命令]
  跑完告诉我结果。

你：
  通过了 / 报错了

Claude：
  [如果报错：修 bug]
  [如果通过：✅ 第 X 步完成。下一步是 Y。是否同意？]
```

---

## 二、项目初始化模板

### 2.1 第一步（永远是这个）

```
第 1 步：项目初始化 + Schema 定义

产出：
① pyproject.toml（含所有依赖声明）
② config/settings.yaml（全局配置模板，含 LLM、搜索、Agent、存储）
③ src/core/schema.py（Pydantic V2 模型，所有 Agent 共享的数据结构）
④ tests/test_schema.py（Schema 单元测试）
⑤ 所有 src/ 子目录 + __init__.py

验证：
pytest tests/test_schema.py -v
python -c "from core.schema import ..."  # 确认导入成功
```

### 2.2 关键约束

- **schema.py 必须最优先写**，所有后续 Agent 都依赖它
- **Pydantic V2 不要用 `class Config`**（V1 语法，V2 已废弃）
- **URL 字段用 `str`**，不要用 `HttpUrl`（序列化会变成 `pydantic_core.Url` 对象，LangGraph State 无法序列化）
- **字段级溯源**：核心字段（strengths/weaknesses）用 `AnnotatedFinding` 绑定 `Evidence`
- **枚举类**：`RejectReason` 用 `str, Enum`，确保 JSON 可序列化

---

## 三、标准开发顺序（按模块递进）

```
第 1 步: Schema（Pydantic 模型，所有 Agent 共享）
第 2 步: Agent 基类 + 消息总线（pub/sub）
第 3 步: Collector/采集 Agent（搜索 + 抓取 + LLM 解析）
第 4 步: Analyst/分析 Agent（功能对比 + SWOT + 市场洞察）
第 5 步: Writer/撰写 Agent（Markdown 报告生成）
第 6 步: Reviewer/质检 Agent（交叉审查 + 条件路由）
第 7 步: Orchestrator/DAG 编排（LangGraph + 顺序 fallback）
第 8 步: 前端（Streamlit/FastAPI）
第 9 步: 工程化（Docker + CI + 文档）
第 10 步: 可观测性（tracer + audit + cost + guardrails）
```

**原则**：
- 每个 Agent 写完立刻写测试，不要在 Agent 间跳跃
- 测试覆盖正常路径 + 降级路径（LLM=None 时的行为）
- 不要跳过任何一步

---

## 四、每次写完代码必须做的验证

```bash
# 1. 全量测试
pytest tests/ -v

# 2. 单模块测试
pytest tests/test_collector.py -v

# 3. CLI 端到端（第 7 步后可用）
python -c "
from core.orchestrator import Orchestrator
config = {'llm': {'api_key': ''}, 'search': {'api_key': ''}}
orch = Orchestrator(config)
result = orch.run('Notion', use_langgraph=False)
print(f'Profiles: {len(result.get(\"competitor_profiles\", []))}')
"

# 4. 导入检查
python -c "from core.schema import CompetitorProfile, Evidence; print('OK')"
```

---

## 五、Bug 库（本项目踩过的所有坑）

### 5.1 数据模型类

| Bug | 现象 | 根因 | 预防 |
|---|---|---|---|
| Pydantic V1 Config 不生效 | `json_encoders` 无效 | V2 用 `model_config` | 全部用 Pydantic V2 语法 |
| `HttpUrl` 序列化异常 | LangGraph State 报 `TypeError` | `pydantic_core.Url` 不是 `str` | URL 字段永远用 `str` |
| `ReviewResult` 缺少路由字段 | 条件路由无法工作 | 没有 `reject_reason` 枚举 | 设计时就考虑状态机的所有转换路径 |
| `AnnotatedFinding` 没有 evidence 绑定 | SWOT 无法溯源 | strengths/weaknesses 是裸 `list[str]` | Schema 阶段就想好溯源粒度 |

### 5.2 LLM 集成类

| Bug | 现象 | 根因 | 预防 |
|---|---|---|---|
| LLM 返回 `[{...}]` 而非 `{"key": [...]}` | `'list' object has no attribute 'get'` | DeepSeek 有时返回 JSON 数组 | `parse_to_profile` 中 `isinstance(raw_json, dict)` 守卫 |
| LLM JSON 含控制字符 | `json.JSONDecodeError: Invalid control character` | LLM 输出含未转义换行符 | `json.loads(text, strict=False)` |
| 占位符字符串含 `[` | 被正则 `\[.*\]` 误匹配为 JSON 数组 | 占位符 `[collector]` 含方括号 | 占位符用 `()` 代替 `[]` |
| 合并 prompt 后 LLM 输出无法解析 | 矩阵全 N/A | 两个独立 prompt 强行合并，LLM 输出格式混乱 | 不要合并结构差异大的 prompt |

### 5.3 字符串/类型类

| Bug | 现象 | 根因 | 预防 |
|---|---|---|---|
| 字符串迭代陷阱 | 输入"飞书"产出 6 个竞品(n,o,t,i,o,n) | `for target in "飞书"` 遍历每个字符 | 入口处 `isinstance(targets, str)` → 包装为 `[targets]` |
| 变量名替换疏漏 | 多竞品只分析第一个 | `orchestrator.py` 传了 `target_product`(字符串) 而非 `products`(列表) | 参数传递层级>1 时用实例变量 `self._products` |
| 中文逗号不识别 | "飞书，Notion" 被当成 1 个竞品 | `split(",")` 不识别 `，` | `target.replace("，", ",").split(",")` |
| TypedDict 导致字段丢失 | `state["target_products"]` 变成字符串 | `state: AgentState = {...}` 类型注解可能截断额外 key | 不用 TypedDict 类型注解，直接用 plain dict |
| `list(products)` 迭代字符串 | `list("飞书")` → `['飞','书']` | `products` 是字符串时 `list()` 迭代字符 | `products if isinstance(products, list) else [products]` |

### 5.4 集成测试类

| Bug | 现象 | 根因 | 预防 |
|---|---|---|---|
| 单测全绿但集成崩溃 | 128 条通过，但真实 LLM 跑不通 | 单测 mock LLM 返回完美 JSON，真实 LLM 可能返回数组/乱码 | 集成测试覆盖真实 LLM + 对抗性输入 |
| Reviewer 死循环 | 3 轮始终驳回 | 信源充足(9 条/9914 字符)但 LLM 误判 `insufficient_source` | 程序化检查信源 >= 3 时覆盖 LLM 判断 |
| 质检驳回后 source_pool 被覆盖 | 补采后反而少数据 | `state.update(result)` 替换而非追加 | 追加模式：`state["source_pool"].extend(new_sources)` |

### 5.5 性能/体验类

| Bug | 现象 | 根因 | 预防 |
|---|---|---|---|
| 页面加载慢 | Streamlit 启动卡 10+ 秒 | Google Fonts `@import` 超时 | 永远用系统字体栈，不要外部字体请求 |
| 报告质量下降 | 矩阵全 N/A + SWOT 空洞 | 合并 prompt 后 LLM 输出格式不可控 | 不合并结构差异大的 prompt；SWOT 优先用 Collector 采集的真实数据 |

---

## 六、开发节奏建议

| 阶段 | 时间 | 产出 |
|---|---|---|
| **Day 1-2** | Schema + Agent 基类 + Collector | 数据流入管道打通 |
| **Day 3-4** | Analyst + Writer | 分析—报告主链跑通 |
| **Day 5** | Reviewer + Orchestrator | 反馈闭环 + 条件路由 |
| **Day 6** | 前端 + Docker + CI | 可演示 + 可部署 |
| **Day 7** | 可观测性 + 测试补全 + 文档 | 生产级工程资产 |

---

## 七、如何传给新终端

### 方法 A：直接复制文件

```bash
# 新项目目录
cp ../AI驱动的竞品分析与agent协作系统/VIBECODING_PLAYBOOK.md ./CLAUDE.md
```

新终端的第一条消息：
```
请先阅读 CLAUDE.md 中的 Vibecoding 实战手册，然后按照 Plan-and-Execute 流程和我协作。第一个需求是：XXX
```

### 方法 B：两个终端并行

```bash
# 终端 1：打开 Claude Code 到新项目
cd ~/desktop/agenthub
claude

# 在 Claude Code 里说：
请先读取这个文件作为协作规则：
C:\Users\10393\desktop\AI驱动的竞品分析与agent协作系统\VIBECODING_PLAYBOOK.md
然后按照其中的 Plan-and-Execute 流程和我协作。
第一个需求是：XXX
```

### 方法 C：写入 CLAUDE.md（推荐）

```bash
# 新项目
cd ~/desktop/agenthub
cp ../AI驱动的竞品分析与agent协作系统/VIBECODING_PLAYBOOK.md ./CLAUDE.md
# 编辑 CLAUDE.md，在末尾追加项目特定信息
```

Claude Code 启动时自动读取 `CLAUDE.md`，不需要每次手动告知。

---

## 八、一句话总结

> **Schema 先行 → Agent 逐个写 → 每步跑测试 → 降级路径必须测 → 字符串别迭代 → 变量名别搞混 → JSON 解析加 strict=False → 不要合并结构差异大的 prompt → 系统字体别远程加载**
