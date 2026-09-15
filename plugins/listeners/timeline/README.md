# timeline —— listener 插件示例

`type: "listener"`：订阅事件总线上的全部观察事件，并追加写入本插件目录的
`timeline.jsonl`（`ts / trace_id / name / payload`）。

## 契约

```json
{
  "type": "listener",
  "events": ["tool.after", "memory.write"],
  "entry": { "module": "listener.py", "factory": "create_listener" }
}
```

- 工厂 `create_listener(plugin_dir)` 返回 `core.events.Subscription` 列表
  （事件名 + handler + priority，与 HookGateway 同一套顺序规则）；
- `events` 声明本插件会订阅的事件（`"*"` 表示全部）；工厂返回未声明的事件会
  启动即报错；
- **决策类事件**（如 `user_prompt.submit`）不允许 listener 订阅——需要影响流程
  请写 hook 插件（`tool_before` 那套契约）；
- 订阅者在主流程内被直接调用：保持轻量，重活自己丢后台任务。

## 用途

- 调试：看一次对话的事件时间线（配合 `trace_id` 串链路）；
- 集成：把 `memory.write` 同步到外部系统、把 `tool.after` 上报统计。

想停用：把 `plugin.json` 的 `enabled` 改成 `false`；想换行为：改 `listener.py`
或整目录替换（保持 name/factory 契约不变）。
