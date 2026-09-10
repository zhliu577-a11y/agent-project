# ADR 0006：事件总线 UNBOX（观察广播 + 决策折叠）

- 状态：已采纳（分两阶段落地）
- 日期：2026-09-10
- 前置：ADR 0001（插件与网关）、`core/hooks.py`、`core/tracing.py`

## 背景

目前系统里只有 HookGateway 的 5 个写死事件（turn/llm/tool 前后），且只对 hook
插件开放。想把“会话开始”“模型请求”“记忆写入”“工具被拒”等事实告诉其它模块
（tracing、audit、API/前端、未来的多 Agent），只能改内核或绕道 hook。

需要一个**进程内的发布/订阅总线**：发布者不知道订阅者，订阅者按需接入。

## 决策

### 1. 事件是结构化数据

```python
Event(name="tool.after", payload={"ctx": …, "tool_call": …, "ok": True},
      trace_id="ab12cd34", ts="2026-09-10T12:00:00+00:00")
```

- 名称用 `<域>.<动作>` 命名（`turn.start`、`model.request`、`tool.after`…）；
- payload 携带对象引用（内核内部使用）；落盘/日志时由 sink 做安全转换与截断。

### 2. 两类语义，严格区分

| 类型 | API | 规则 |
|---|---|---|
| 观察类 | `publish(event)` | 只读广播；异常隔离；不允许改变流程；支持 `subscribe("*")` |
| 决策类 | `decide(event) -> allow/ask/deny` | 汇总订阅者表态，按 `deny > ask > allow` 折叠；异常按 deny |

决策类事件（如 `tool.before`、`user_prompt.submit`）**必须**由内核采纳结果；
观察类事件**禁止**成为隐式控制流。

### 3. 与 HookGateway 的演进关系（不并行两套）

分两阶段，避免破坏既有决策语义：

- **阶段一（本期）**：总线作为观察层落地；loop 把 `turn.start / model.response /
  tool.after / turn.end` 发布到总线，`HookGateway.attach(bus)` 订阅这些事件并
  扇出给现有 hook 插件（旧插件零迁移）；**决策类 `tool.before` 仍由
  HookGateway 直接承担**（ask-once 语义保持在内核）；
- **阶段二（未来）**：把决策类事件也迁到 `bus.decide`，HookGateway 成为纯适配
  订阅者；迁移前提是事件 payload 稳定且有回归测试兜底。

### 4. 新增事件清单（阶段一）

| 事件 | 类型 | 发布点 |
|---|---|---|
| `turn.start` / `turn.end` | 观察（同时桥接 hook） | loop |
| `model.request` / `model.response` / `model.error` | 观察 | loop |
| `tool.after` | 观察（同时桥接 hook） | loop（含被拒工具） |
| `tool.denied` | 观察 | loop |
| `memory.write` / `memory.update` / `memory.delete` | 观察 | MemoryGateway |
| `session.start` / `session.end` | 观察 | CLI / API |
| `user_prompt.submit` | **决策** | CLI / API（deny 即拒绝本轮） |

### 5. 落盘与调试

- 设置 `EVENT_LOG=<path>` 时订阅 `*`，把事件写成 JSONL（payload 安全转换 + 截断），
  为前端时间线与未来回放打基础；
- 事件默认带当前 `trace_id`，与日志追踪一致。

## 边界

- 进程内总线，不做跨进程消息队列；
- 不做回放引擎（先记录，后评估）；
- 观察类处理器不得抛错影响主流程（异常隔离 + 日志）；
- 订阅顺序按 `(priority, 注册顺序)`，与 HookGateway 一致。

## 落地清单

1. `core/events.py`：`Event` / `EventBus` / `Decision` / JSONL sink；
2. `HookGateway.attach(bus)`：观察事件 → 现有 hook 扇出；
3. `loop.run_agent(events=…)`：发布阶段一事件；未传 bus 时行为与现在完全一致；
4. `main.py` / `api/main.py`：创建总线、挂 sink、发布 session 事件、
   `user_prompt.submit` 决策；
5. `MemoryGateway(events=…)`：发布记忆写/改/删事件；
6. 测试（顺序/隔离/折叠/trace/桥接/loop 事件序列/记忆事件）+ 文档。

落地记录（阶段一已完成）：

- `core/events.py` + HookGateway 桥接 + loop/main/api 发布点 + MemoryGateway 事件；
- listener kind（`plugins/listeners/timeline` 示例）：订阅者插件化，
  `events` 声明校验、决策类事件拒绝订阅、退订令牌预留；
- 回归 149 tests，ruff clean。
