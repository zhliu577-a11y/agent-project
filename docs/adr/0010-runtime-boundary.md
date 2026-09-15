# ADR 0010：HarnessRuntime 与运行状态隔离

- 状态：已采纳
- 日期：2026-09-15

## 背景

插件生命周期落地后，`main.py` 仍直接负责装配插件、创建网关、选择模型和存储，
并进入交互循环。这带来两个问题：

1. CLI、未来网页和其它客户端无法复用同一套运行时装配；
2. registry 的 `enabled` 只表达用户期望，无法回答当前进程是否真的加载成功。

## 决策

新增 `HarnessRuntime`，作为可运行对象图的唯一装配边界。它负责：

- 读取 `PluginManager` 的 enabled records；
- 分别装配内置插件和每个外部插件包；
- 创建 HookGateway、EventBus、Model、Session、Memory、SkillGateway、McpGateway
  和 ToolRegistry；
- 暴露 `start()`、`close()`、`ready` 与 JSON-safe 的 `snapshot()`；
- 把外部包的实际运行结果写回 `PluginManager`。

`main.py` 只负责配置、启动 Runtime 和交互循环，不再感知具体插件类别。

外部插件按包建立隔离边界：

```text
assemble_plugins(builtin/)
assemble_plugins(installed/package-a/1.0.0/)
assemble_plugins(installed/package-b/2.0.0/)
→ 合并并检查 contribution 重名
```

单个外部包装配失败时，该包写为 `runtimeStatus=error` 和 `lastError`，其它包继续。
如果当前配置选中的 model/session/memory 恰好来自失败包，Runtime 启动会失败。

状态语义：

| 状态 | 含义 |
|---|---|
| `disabled` | registry 中 `enabled=false` |
| `idle` | 已启用，等待本次 Runtime 装配 |
| `active` | 本次 Runtime 已成功装配 |
| `error` | 本次装配失败，`lastError` 保留原因 |
| `stopped` | 成功启动过的 Runtime 已正常关闭 |

## 后果

- loader 仍然只读取 `plugin.json`，不写生命周期状态。
- Runtime 是唯一持有真实 gateway、连接和工具表的对象。
- registry 的生命周期写入仍经过 `PluginManager`，Runtime 不直接改 JSON。
- `python cli.py plugin list` 可以同时展示期望状态和最近运行状态。
- 第一版不支持热加载；重新启用、升级或卸载后需要重建 Runtime。
- MCP 连接仍是在模型调用 `use_plugin` 时建立，`active` 表示插件已装配，
  不等价于 MCP 子进程已经连接。
