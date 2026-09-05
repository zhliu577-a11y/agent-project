# ADR 0001：插件目录 + 双网关（MCP / Hooks）

- 状态：已采纳
- 日期：2026-09-04
- 背景来源：Harness 工程（Claude Code / DeepSeek Harness）的组织范式

## 背景

原实现把扩展点散落在根目录：`mcp_servers.json` 管 MCP 服务器清单、
`permission.py` 管权限钩子、`mcp_bridge.py` 直接持有全部 MCP 连接、
loop 通过 `use_server` 让模型逐台加载服务器。扩展一个新能力需要改多个文件，
且 Agent 与 MCP 世界之间没有清晰的“单一通道”边界。

目标：

1. 插件“拖入文件夹即可用”——一切插件放入 `plugins/`，自描述、可独立审阅；
2. MCP 工具调用改为网关形态：Agent 只与网关交互，网关维护各 MCP 插件的
   连接/命名/调用/清理；
3. hooks 同样插件化，所有钩子由统一的钩子网关扇出。

## 决策

### 1. 插件目录与清单

- 插件根目录 `plugins/`；按类别组织子目录（`mcp/`、`hooks/`），发现逻辑
  按目录树递归查找 `plugin.json`，不依赖固定类别路径（未来类别同理）；
- 每个插件 = 一个目录 + `plugin.json`（name/type/version/description/enabled/
  entry）+ 自身代码或配置；
- 清单错误启动即报错；`enabled: false` 的插件同样校验结构但不加载；
- 目前 `type` 支持 `mcp`、`hook`；新类别只需扩展
  `plugins/loader.py` 的 `SUPPORTED_KINDS` 与对应加载器。

### 2. 钩子网关（HookGateway）

- `core/hooks.HookGateway` 是内核唯一钩子入口：按注册顺序扇出
  turn/llm/tool 生命周期事件，异常隔离；`tool_before` 决策采用
  “任一拒绝即拒绝、异常按拒绝处理”。
- 钩子插件契约：`entry.module + entry.factory`，加载器动态导入并调用
  `factory(plugin_dir)`，要求返回 `LifecycleHooks`。

#### 2.1 执行顺序与决策语义（2026-09-05 补充）

- 清单新增可选 `priority`（默认 0），钩子按 `(priority, 注册顺序)` 升序执行，
  同优先级按名字典序装配，保证跨启动稳定；
- `tool_before` 的返回从布尔升级为三方表态 `allow / ask / deny`；网关收集
  **全部**钩子表态后按 `deny > ask > allow` 折叠（对齐 dsh hook outcome 语义）；
- 不做“首个拒绝即短路”，让审计类钩子也能看到被拒尝试；
- 折叠结果为 `ask` 时只向用户确认一次（`confirm` 可注入以便测试/非交互环境）；
- 钩子异常按该钩子表态 `deny` 处理，继续评估其余钩子。

### 3. MCP 网关（McpGateway）

- `gateways/mcp_gateway.McpGateway` 是内核与 MCP 世界之间的唯一通道：
  连接、超时、失败清理、关闭全部封装在网关内；
- 工具以 `<插件名>__<工具名>` 命名空间暴露，天然避免多插件同名工具冲突，
  命名稳定可预测（未做 dsh 式超长名截断哈希，插件名已限制为安全字符集）；
- 模型通过 `use_plugin` 让网关挂载插件；`mount` 幂等；
- **本阶段网关是进程内聚合器**：子 MCP 服务器仍以 stdio 子进程方式由网关
  拉起。这满足“内核只面向一个网关”的边界；物理上 Agent 进程内仍会派生多个
  子进程，但 loop 不再感知。

## 备选方案与取舍

### A. 进程外 MCP 网关（代理进程）

另起一个进程实现 MCP Server，聚合各插件后让 Agent 通过一条 stdio/HTTP 连接
访问。好处是“Agent 与网关之间真的只有一条线上连接”，并允许第三方客户端复用
同一网关。代价：多一跳进程间通信、需要实现 MCP 代理语义（tools/list 聚合、
调用转发、子进程生命周期），当前单机单 Agent 场景收益有限。

决定：先以进程内网关 + 窄接口落地，把“换成进程外网关”的改动面收敛在
`McpGateway` 一个对象上（接口只有 available/mount/close/tool 视图）；
等出现远程 Agent 或多客户端需求时再做 ADR 评估。

### B. 每插件一个客户端插件（dsh 式）

DeepSeek Harness 为每个 MCP Server 实例化一个 client 插件。该模型更灵活
（可热插拔、每服务器独立重连），但要求底层有 Cordis 那样的插件树/共享
上下文机制；本仓库目前不需要，且用户明确要求“单一网关”边界。

## 后果

正面：

- 新增 MCP 工具 = 拷贝一个插件目录并改 `plugin.json`；新增行为 = 拷贝一个
  hook 插件目录；内核零改动；
- 工具命名空间化让权限策略按插件精确配置（`filesystem__delete_file`）；
- 旧的 `mcp_servers.json / permission.py / mcp_bridge.py / use_server`
  全部被新结构取代。

代价/待办：

- 命名空间变化使既有权限规则需要按新工具名重写（已随 permission 插件更新）；
- 进程内网关意味着 MCP 子进程仍在 Agent 进程内派生，进程隔离弱于代理进程
  （见备选 A）；
- 钩子目前只覆盖 loop 内五个时机，尚无用户输入提交前等更细拦截点
  （Roadmap）。
