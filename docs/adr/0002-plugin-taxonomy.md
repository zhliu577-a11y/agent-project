# ADR 0002：万物皆插件——插件类型注册表与目标形态

- 状态：已采纳（M0 / M1 / M2 已完成）
- 日期：2026-09-05
- 背景来源：用户目标 = Harness 工程形态（Claude Code / DeepSeek Harness）：
  **除 Agent loop 外，万物皆插件**

## 背景

当前 `mcp` / `hook` 两类插件已落地（ADR 0001）。但仓库里仍存在固定子系统：

- `models/` 是写死的模型适配器，换模型要改代码；
- 系统提示词在 `main.py` 里手工拼插件目录，插件一多就会上下文膨胀；
- 没有本地工具形态：一切工具能力都要背一个 MCP 子进程；
- 没有 Skills / 会话持久化等类别，而它们已在 Roadmap 上。

若不先把“插件类型”本身做成可注册的机制，每新增一类插件就要改
`plugins/loader.py` 的分支和 `main.py` 的装配，插件体系会随类型增长而腐化。

## 决策

### 1. 内核边界：唯一固定的是 loop

- `loop.py` 与 `core/types.py`（消息/上下文等纯数据）是唯一固定部分；
- `core/` 其余文件只承担**接口契约**职责：`Tool`、`LifecycleHooks`、
  `ModelAdapter`，以及未来的 Skill/Session 协议；
- 内核通过“注册表/网关”取服务，不直接 import 任何插件实现。

### 2. 插件类型注册表（Kind Registry）

`plugins/loader.py` 从“按 type 写 if-else”升级为注册表：

```python
PLUGIN_KINDS: dict[str, KindLoader] = {
    "hook": load_hook_plugins,  # 实例装进 HookGateway
    "mcp": load_mcp_plugins,  # 规格交给 McpGateway
    "tool": load_tool_plugins,  # （新增）本地工具装进 ToolRegistry
    "model": load_model_plugins,  # （新增）模型适配器
    "skill": load_skill_plugins,  # （未来）指令集目录
}
```

- 每种 kind 有独立的：清单 schema 校验、加载函数、贡献目标（owner）；
- 新增一种类型 = 注册一行 + 实现一个 loader，不再改动装配主流程；
- 未知 type 启动即报错并列出已支持类型（保持“配置错误不静默”原则）。

### 3. 插件类型清单与归属（目标形态）

| type | 是什么 | 贡献给谁 | 现状 |
|---|---|---|---|
| `hook` | 生命周期行为 | HookGateway | 已落地 |
| `mcp` | 进程外/外部工具 | McpGateway（懒挂载） | 已落地 |
| `tool` | 进程内轻量工具 | ToolRegistry（启动即注册） | 已落地（M1） |
| `model` | LLM 适配器 | 当前激活的模型实例（AGENT_MODEL 选定） | 已落地（M2） |
| `skill` | 按需注入的操作指令 | Skill 目录/渐进披露 | 待做 |
| `session` | 会话持久化/恢复 | SessionStore | 未来评估 |

约定：

- 命名澄清：`core/tool.py` 的 `Tool` 是**内核接口契约**（不是插件类别）；
  `McpTool` 是 MCP 工具的进程包装；`type: "tool"` 专指**本地工具插件类别**
  （`plugins/tools/*`，进程内函数，启动即注册）。三者层级不同，避免混淆；
- 插件名同 kind 内全局唯一；工具统一命名空间 `<插件>__<工具>`；
- 权限闸门对**所有**工具生效（本地 tool 与 MCP 工具同等对待）；
- 模型 kind 允许多插件共存，由环境变量/配置选出激活者；
- 默认模型插件（deepseek）与原 `models/` 行为保持向后兼容。

### 4. 统一装配（Harness 启动器）

`main.py` 退化为固定五步，不感知具体插件：

1. 扫描 `plugins/`，得到全部清单；
2. 按 kind 注册表分派给各 loader；
3. 把产物装进对应网关/注册表（HookGateway、ToolRegistry、模型选择器……）；
4. 系统提示词只放“目录清单”，大目录由模型按需读取（渐进披露，配合 skill 类）；
5. 进入固定 loop。

## 里程碑

- **M0**：loader 改造为 kind 注册表 + 统一装配（地基，改动集中、可独立测试）；
- **M1**：本地工具 kind（`plugins/tools/*`，无进程、直接函数）；
- **M2**：模型 kind（`models/` 迁移为 `plugins/model/deepseek` 等，可配置切换）——已完成；
- **M3**：Skill kind + 插件目录清单工具（渐进披露，替代硬拼 system prompt）；
- **M4**（未来）：session 持久化 kind、HTTP MCP、插件目录热加载（watch）。

进度：M0（kind 注册表 + `PluginAssembly` 统一装配）、M1（本地工具 kind，
`plugins/tools/text|json` 示例）、M2（模型 kind，`plugins/model/deepseek` +
`AGENT_MODEL` 选择）均已完成。“除 Agent loop 外万物皆插件”在本仓库闭环：
模型、工具（本地 + MCP）、行为（hooks）全部来自插件目录。

## 后果

正面：

- 每类插件的生命周期与贡献点有固定位置，代码库不再随类型数量膨胀；
- 换模型、加本地工具、接 Skills 都不再动内核；
- 所有工具的权限/审计钩子天然统一。

代价/待办：

- 模型选择目前是进程级 `AGENT_MODEL`，会话内动态切换模型尚未实现（未来评估）；
- 渐进披露要定目录工具的参数与缓存规则，避免每轮重复拉取；
- 本 ADR 状态待用户确认后再把“提议”改为“已采纳”。
