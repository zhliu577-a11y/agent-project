# ADR 0004：记忆分层与 Session 插件（短期记忆落地）

- 状态：已采纳（短期/长期记忆与上下文模块均已落地；HTTP 接口已提供）
- 日期：2026-09-05
- 目标原则：**除 Agent loop 外万物皆插件**

## 背景

当前 agent 有两个记忆缺口：

1. **同一会话内没有多轮记忆**：每次用户输入都会重建
   `[system, user]`，上一轮的 assistant/tool 消息不会进入模型上下文；
2. **跨重启没有记忆**：对话只活在内存里，退出即丢失。

记忆不是单一机制，本 ADR 明确分层，避免把短期与长期塞进同一个组件：

| 层 | 内容 | 载体 |
|---|---|---|
| 短期记忆 | 当前会话消息历史 + 上下文预算 | session kind（本期落地） |
| 长期记忆 | 跨会话语义笔记（事实/偏好/结论） | memory kind（本期只定边界） |
| 程序记忆 | 做事规则/技能 | skill / hook kind（已落地） |
| 情景记忆 | 事件流可回放 | audit hook / session 事件（已部分具备） |

## 决策

### 1. 短期记忆 = 会话上下文 + 持久化，均插件化

- 内核 `run_agent` 增加 `history` 参数：同一会话内把历史消息带进模型上下文，
  修复“多轮即失忆”；
- 新增 `session` kind：存储后端插件（`plugins/session/*`），
  实现 `core.session.SessionStore`：

```python
class SessionStore(ABC):
    async def load(self, session_id: str) -> list[Message]: ...
    async def save(self, session_id: str, messages: list[Message]) -> None: ...
```

- 存储后端随插件走：`SESSION_STORE`（默认 `jsonl`）选择激活插件，语义与
  `AGENT_MODEL` 一致——换存储 = 复制一个 session 插件目录；
- 首个示例插件 `plugins/session/jsonl`：每条消息一行 JSON，
  数据目录 `SESSION_DATA_DIR`（默认项目下 `.sessions/`，gitignore）；
- 会话标识 `SESSION_ID`（默认 `default`），同一 id 自动恢复历史。

### 2. 长期记忆 = 独立 memory kind（已落地）

- 长期记忆解决“跨会话事实”，不是把历史塞回去；
- 未来 `plugins/memory/*` 实现 `MemoryStore`（笔记式键值/文档），
  暴露 `remember` / `recall` 工具，并在会话开始时选择性注入；
- 已落地：`core.memory.MemoryStore` + `plugins/memory/sqlite`（生产默认，
  事务/WAL/参数化 SQL）+ `plugins/memory/jsonl`（可读示例）+
  remember / recall / forget 工具（MEMORY_STORE 选择后端）；
- 语义检索已落地：`plugins/memory/vector` + embedding kind
  （`plugins/embedding/debug|openai-embedding`），弱结果自动回退字面搜索；
- memory kind 加入 kind 注册表时，仅需新增 loader + 一个网关，内核不变。

### 3. 上下文预算（已落地，策略仍可替换）

- `core/context` 提供 token 估算与“丢最旧、保最近”的裁剪，chat/API 在
  每轮请求前按 `CONTEXT_MAX_TOKENS` 裁剪；
- SessionStore 只负责“存/取”，策略与存储解耦；摘要压缩策略可后续以
  hook 插件形式替换/叠加。

### 4. HTTP 接口（api/）

- FastAPI 无头 harness：`POST /chat`、会话历史、记忆 CRUD、插件概览；
- Web 无交互弹窗，ask 权限默认按拒绝处理。

## 后果

正面：

- 多轮对话有了真正的连续性；重启后同 `SESSION_ID` 可续聊；
- 短期/长期/程序/情景四类记忆各有明确 owner，互不耦合；
- 换存储后端、换压缩策略都不动内核。

代价/待办：

- JSONL 全量覆写实现简单但数据量大时低效（后续可增量 append）；
- 会话开始时的“记忆自动注入”仍未做（模型需要时用 recall 主动查）；
- CLI（main.py）与 API（api/main.py）的装配逻辑存在少量重复，后续可抽
  公共 runtime。
