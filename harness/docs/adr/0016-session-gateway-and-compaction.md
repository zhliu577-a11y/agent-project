# ADR 0016：SessionGateway 事务边界与 Compaction 插件

## 背景

原来的 `session` 只有全量 `load / save`。Gateway 只是绑定 `session_id` 的
转发器，无法提供 append、并发版本检查、checkpoint 恢复或持久化压缩。
把压缩写进某个存储后端又会让 JSONL、SQLite、Redis 等实现重复承载策略。

## 决策

短期记忆拆成三个独立层次：

1. `SessionStore`：只负责消息和 checkpoint 的持久化；
2. `SessionGateway`：负责锁、revision、原子提交、回滚、事件和压缩编排；
3. `CompactionPolicy`：受控 `compaction.v1` kind，只生成压缩后的替换历史。

```text
commit_turn(messages, checkpoint)
  -> read current revision
  -> compaction.compact(snapshot)
  -> validate replacement
  -> store.replace(expected_revision=current)
  -> store.save_checkpoint()
```

压缩插件不能直接写 Session。只有 Gateway 在 revision 仍匹配时才能提交，
失败时保留旧历史和旧 checkpoint。

内置 JSONL 和内存后端声明 `supports_revisioning = True`。只实现旧
`load / save` 的 v1 插件保持兼容，但不提供跨进程乐观锁。

## 结果

- `context` 决定模型本次看到什么，不修改持久化历史；
- `ContextGateway` 只读召回长期记忆，是否注入以及占用多少预算由被选中的
  `context` 插件决定；默认 `memory-tail-window` 会消费该召回结果；
- `session` 决定历史如何存储，不决定压缩策略；
- `compaction` 决定何时压缩和替换成什么；
- `context` 和 `compaction` 都不直接写长期记忆；自动提取记忆应使用独立的
  提取流程调用 `MemoryGateway`；
- 同一领域插件目录可以按 `plugins/session/*` 分组，但类型仍由
  `plugin.json.type` 决定；
- 内置 `rolling-summary` 使用抽取摘要，后续可增加模型摘要、SQLite、
  Redis 或分支检查点插件而不改 Agent loop。
