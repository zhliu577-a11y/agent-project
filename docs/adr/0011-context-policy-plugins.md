# ADR 0011：上下文策略插件

- 状态：已采纳
- 日期：2026-09-15

## 背景

原先的上下文处理写在 CLI 每轮输入之前，只根据消息文本估算 token，并从最旧
消息开始删除。这带来四个问题：

- system prompt、工具 schema 和当前 turn 没有计入预算；
- 一个 turn 内工具循环新增的消息不会再次裁剪；
- 简单裁剪可能拆开 `assistant(tool_calls)` 和对应 `tool` 结果；
- 裁剪结果被写回 Session，永久丢失原始历史。

不同模型、不同会话场景需要不同策略，例如固定尾窗、摘要、事实保留、按模型
预算拆分等，因此不应继续把一种裁剪算法写死在 loop。

## 决策

新增受控 kind `context`：

```python
class ContextPolicy(Protocol):
    async def prepare(self, request: ContextRequest) -> ContextResult: ...
```

- `ContextRequest` 包含完整消息、工具 schema、最大 token、session id、turn
  和运行状态；
- `ContextResult` 是本次模型调用可见的消息视图，不写回 Session；
- Runtime 按 `CONTEXT_STRATEGY` 从 `plugins/context/*` 选择策略；
- loop 在每次 `model.complete()` 前调用策略，因此同一 turn 的工具循环也会
  重新计算上下文；
- Session 始终保存完整原始消息，裁剪或摘要只影响模型请求；
- 宿主保留安全兜底：策略异常、返回非 `ContextResult`、丢失 system、产生孤儿
  tool 消息或仍超预算时，回退到内置 `tail-window`；
- 工具调用与结果按原子组裁剪，不能只删除其中一条；
- 摘要策略如调用模型，必须使用独立预算，不能递归进入完整 agent loop。

默认插件 `plugins/context/tail-window` 保留 system prompt 和最新完整消息组，
并计入工具 schema 的估算成本。内置的第二策略 `summary-window` 会把较早消息
抽取成有限长度摘要并并入 system prompt，再保留最新完整消息组。

## 后果

- 更换上下文策略只需新增或启用一个 `context` 插件并修改配置；
- Session 历史不再因上下文预算而永久截断，可换用不同策略回放同一会话；
- loop 仍掌握模型调用时机和安全边界，插件只负责选择或压缩上下文；
- 第一版不自动摘要、不做跨请求缓存，也不承诺 token 估算与真实 tokenizer
  完全一致；插件可以在自己的 `ContextRequest` 与结果 metadata 中扩展策略。
