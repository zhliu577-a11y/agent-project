# ADR 0013：插件平台协议（Manifest / Capability / Lifecycle / Protocol）

- 状态：已采纳
- 日期：2026-09-15
- 前置：ADR 0002（kind 注册表）、ADR 0007（contribution）、ADR 0008（包生命周期）

## 背景

插件能力已经拆到 `plugins/`，但插件体系本身仍需要一套统一、可验证的协议。
仅靠 Python 工厂函数和目录约定，无法稳定回答四个问题：

1. 这个插件声明了什么，是否允许被装载？
2. 它实现的是哪一种能力契约，产物类型是否正确？
3. 它在运行时何时初始化、启动和停止，失败如何回滚？
4. 插件与宿主、不同版本宿主之间如何协商兼容性？

## 决策

### 1. Manifest 是唯一声明入口

单能力插件使用 `type + entry`；多能力包使用 `apiVersion + contributes[]`。
清单在发现阶段完成结构校验，不导入插件代码。

仓库内置插件都必须显式声明：

```json
{
  "apiVersion": "1",
  "protocolVersion": 1,
  "name": "text",
  "type": "tool",
  "version": "1.0.0",
  "contract": "tool.v1",
  "entry": {
    "module": "tool.py",
    "factory": "create_tools"
  }
}
```

`apiVersion` 表示 manifest schema 版本；`version` 表示插件自身版本；
`protocolVersion` 表示宿主与插件之间的执行协议版本。三者不能混用。

加载器会把缺失的 `protocolVersion` 规范化为该 kind 的首选版本，作为迁移兼容层。
`contract` 仍可推导，但 `KindHandler.explicit_contract=True` 的 kind（当前是
Skill）必须显式声明，避免高信任能力依赖隐式契约。

### 2. Capability Contract 由 kind 注册表验证

每个受信任的 `KindHandler` 声明首选 `protocol_version`，并可通过
`protocol_versions` 声明一个受支持的版本集合。宿主只接受清单中显式声明或
按首选版本推导出的、该 kind 已支持的版本。
装载器根据 `type + protocolVersion` 得到规范契约名，例如 `hook.v1`、`tool.v1`。

如果清单显式写了 `contract`，必须与宿主计算结果一致；要求显式契约的 kind
缺失该字段时会在 discovery 阶段失败。
`KindHandler.load()` 继续负责产物类型校验，例如：

- `hook` 必须返回 `LifecycleHooks`
- `tool` 必须返回 `Tool` 或 `Tool` 列表
- `model`、`session`、`memory`、`embedding`、`context`、`compaction` 必须返回对应接口实例

因此，Manifest 只声明意图，Capability Contract 负责验证宿主可接受的实现边界。

### 3. Lifecycle 是显式且可回滚的

一次 Runtime 装配经过以下阶段：

```text
discover
  -> validate manifest
  -> negotiate protocol
  -> load factory
  -> instantiate selected capability
  -> register into gateway/registry
  -> setup(context)
  -> start()
  -> ready
  -> stop()
```

只有当前被选中的 model、context、session、memory，以及实际加载的 hook、tool
实例进入运行生命周期。未被选中的替代插件可以保持惰性，不产生副作用。

`setup(context)`、`start()`、`stop()` 都是可选方法。
`PluginLifecycleManager` 按装配顺序调用 setup/start；关闭或启动失败时按逆序 stop。
如果 `setup()` 在分配部分资源后失败，当前插件也会收到 `stop()`，因此生命周期
实现必须保证 `stop()` 可重复调用且能安全清理未完成初始化。
插件必须在 `stop()` 中释放自己创建的资源。

### 4. Versioned Protocol 在装载前协商

宿主内置插件当前使用：

```text
manifest apiVersion: "1"
plugin protocolVersion: 1
```

发现阶段会拒绝未知 `apiVersion`、kind 不支持的 `protocolVersion` 和契约不匹配，
而不是等到插件函数执行后才失败。多版本协商采用“清单声明精确版本、宿主按 kind
校验支持集合”的模型；清单省略版本时使用该 kind 的首选版本，不做隐式降级。

未来新增协议版本时，应同时提供迁移策略：

1. 保留旧版本的适配器；
2. 明确新版本是否兼容旧版契约；
3. 在 `KindHandler` 注册表中增加版本；
4. 为旧版和跨版本组合补测试。

## 配置目录

运行配置统一放在 `config/`：

```text
config/
├── config.json              # 整体默认配置
├── model.json               # 分域配置
├── session.json
├── memory.json
├── embedding.json
├── context.json
├── mcp.json
├── skill.json
└── plugins/
    ├── hook/permission.json # 单插件私有配置
    ├── tool/<name>.json
    └── ...
```

合并优先级为：

```text
环境变量 > 分域/单插件配置 > config/config.json > 代码内置默认
```

`PluginContext.config` 是插件读取私有配置的只读视图。插件不应自行扫描
`plugins/` 或读取其他插件的配置。

## 结果

- 插件是否能装载，在代码执行前就能判断；
- 插件类型不再只靠对象形状猜测，契约错误会确定性失败；
- 运行资源有明确的启动和关闭顺序，启动失败可回滚；
- 宿主升级时可以按协议版本迁移，而不是静默破坏旧插件；
- 中央配置和插件私有配置分层管理，便于 CLI 和未来的网页前端复用。
