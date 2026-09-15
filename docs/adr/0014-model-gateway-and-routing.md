# ADR 0014：ModelGateway、路由与模型资源生命周期

- 状态：已采纳
- 日期：2026-09-15
- 前置：ADR 0001（网关边界）、ADR 0013（插件平台协议）

## 背景

模型 provider 已经以 `plugins/model/*` 形式存在，但 Runtime 原先只完成一次
“选中插件并实例化”。这会让以下能力分散到 Runtime 或前端：

- 运行时切换模型；
- 按 agent role 选择模型；
- 默认模型与 fallback；
- provider 预热、卸载与客户端关闭；
- provider 能力元数据和路由策略；
- 并发请求下的资源引用管理。

如果把这些逻辑直接写进 agent loop，loop 会再次感知具体 provider、路由和重试
策略，破坏“固定 Harness + 可替换能力插件”的边界。

## 决策

### 1. loop 仍只依赖 ModelAdapter

运行链路固定为：

```text
agent loop -> ModelAdapter.complete()
           -> ModelGateway
           -> provider plugin
```

`Runtime.model` 指向 `ModelGateway`，loop 不导入 `ModelGateway`，也不知道
provider 名称、路由规则或 fallback 的存在。

### 2. Gateway 负责 provider 资源缓存

provider 默认惰性创建。`use(prewarm=False)` 只切换显式模型，不提前初始化；
`prewarm()` 用于启动时建立客户端并执行 `setup(context)` / `start()`；
`unload()` 会等待该 provider 的在途请求释放引用后再调用 `stop()`。

Runtime 启动时预热默认模型和 `model.keepWarm` 中的 provider，关闭时统一执行
`ModelGateway.close()`。provider 的 `stop()` 必须可重复调用。

### 3. 路由在每次 complete 前执行

默认路由顺序为：

```text
显式 use 覆盖 -> agent role route -> 默认模型 -> fallback
```

受信任的 `model-router.v1` 插件可以替换该策略。路由请求包含消息、工具 schema
以及所有 provider 的只读能力元数据；路由结果只能返回已装配的 provider 名称。

路由插件属于受控 kind，与普通插件使用相同的 Manifest、Capability Contract、
Lifecycle 和 Versioned Protocol。插件不能自行注册新的 kind。

### 4. fallback 不得混合流式输出

Gateway 按候选顺序尝试 provider。若 provider 在发出第一个 token 前失败，可以
继续尝试下一候选；若已经调用 `on_token`，后续失败直接返回 `ModelError`，
不再 fallback。这样同一条用户回复不会混合两个模型的部分输出。

### 5. provider 配置与能力声明分离

provider 通过 `PluginContext.config` 读取私有配置，环境变量仅作为兼容
fallback。模型能力元数据放在 `plugin.json` 的 `model` 块，供路由决策使用；
provider 本身仍只负责协议传输和响应解析。

## 结果

- agent loop 保持不变，模型层增强不会扩散到编排核心；
- 默认模型、role 路由、显式切换和 fallback 使用同一执行入口；
- provider 初始化与回收有明确的所有者和顺序；
- 多 provider 不会在每轮请求重复建连，也支持显式释放资源；
- 路由能力可替换，但不能绕过受控插件 kind 和 Runtime 装配；
- `unload()` 与 `close()` 对在途请求采用引用计数等待，避免关闭正在使用的客户端。

## 后续

- 基于能力元数据增加质量、成本和延迟策略路由；
- 为 provider 增加健康检查与熔断状态；
- 将模型资源状态接入事件总线和前端管理页；
- 在多 agent 场景中把 role 绑定到每次调用 context，保持共享模型池。
