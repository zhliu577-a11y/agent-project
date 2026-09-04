# agent-project

一个“固定 Harness + 一切皆插件”的精简 agent：**内核（loop）只做编排，
能力全部来自插件目录，插件通过网关接入**。

本仓库借鉴 Harness 工程（Claude Code / DeepSeek Harness 一脉）的组织方式：
生命周期行为做成 **hooks 插件**，外部工具做成 **MCP 插件**；内核不直接接触
任何插件实现，只面向两个网关——`HookGateway`（钩子网关）与 `McpGateway`
（MCP 网关）。

## 特性

- 固定异步 agent loop（调模型 → 执行工具 → 回填 → 判断结束），支持流式输出与多个工具的并行执行
- **插件目录（drop-in）**：`plugins/` 下每个插件是一个自包含目录 + `plugin.json`，
  拖入即可被 Agent 发现；目前支持 `mcp` 与 `hook` 两类
- **MCP 网关**：Agent 只面向网关这一个通道；网关统一维护各 MCP 插件的连接、
  会话、命名与清理，工具名带命名空间（`<插件名>__<工具名>`）
- **钩子网关**：生命周期钩子全部插件化（`turn_start / llm_response /
  tool_before / tool_after / turn_end`），内核只面向 `HookGateway`
- 按需挂载：模型通过 `use_plugin` 让网关挂载插件，避免无谓的进程与上下文开销
- 权限钩子插件示例：`allow / ask / deny` 策略随插件文件夹走，支持通配符
- 配置文件带 schema 校验：写错清单/策略启动即报错，绝不静默出错
- 单元测试 + ruff 规范 + GitHub Actions CI

## 架构

```text
┌──────────────────────── 内核（固定，不随插件变化）───────────────────────┐
│  loop.py       固定循环：调模型 → 工具 → 回填                           │
│  core/         类型与接口：types / Tool / ToolRegistry / model          │
│                钩子网关：core/hooks.HookGateway                         │
└───────┬──────────────────────────────┬─────────────────────────────────┘
        │ 生命周期事件                    │ 工具 schema / 调用
┌───────▼──────────────┐   ┌────────────▼───────────────────────────────┐
│  HookGateway         │   │  ToolRegistry + UsePlugin                  │
│  （钩子网关）         │   │      │                                     │
│  扇出给所有钩子插件    │   │      ▼                                     │
└───────┬──────────────┘   │  McpGateway（MCP 网关：唯一 MCP 通道）       │
        │                  │      │ 连接/维护/清理                        │
┌───────▼──────────────┐   ┌──────▼──────────────────────────────────────┐
│  plugins/hooks/*     │   │  plugins/mcp/*（time / math / filesystem…） │
│  permission 等钩子插件 │   └────────────────────────────────────────────┘
└──────────────────────┘
```

运行链路：启动时扫描 `plugins/` → 钩子插件实例化进 `HookGateway`、MCP 插件
只读清单 → 模型决定调 `use_plugin` → 网关连接对应 MCP 插件并注册命名空间工具
（先过权限钩子闸门）→ 结果回填 → 模型给出最终回答。

## 快速开始

要求：Python 3.13+，Windows / macOS / Linux。

```powershell
python -m venv .venv
.venv\Scripts\activate          # Windows；macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

复制 `.env.example` 为 `.env`，填入 `DEEPSEEK_API_KEY`，然后：

```powershell
.venv\Scripts\python.exe main.py
```

启动日志会列出已发现的插件；对话里让模型“查询当前时间”，它会先 `use_plugin`
挂载 time，再调用 `time__get_current_time`。

## 插件目录（如何加插件）

```text
plugins/
├── mcp/                        # MCP 工具插件
│   ├── time/                   #   每个插件一个目录
│   │   ├── plugin.json         #   清单（名字/类型/入口）
│   │   └── server.py           #   插件自带代码/配置
│   ├── math/
│   └── filesystem/             #   也可以是外部 MCP（npx 等）的启动配置
└── hooks/                      # 生命周期钩子插件
    └── permission/             #   权限策略示例（allow/ask/deny）
        ├── plugin.json
        ├── hook.py             #   LifecycleHooks 实现 + create_hook 工厂
        └── permission.json     #   策略文件随插件走
```

### `plugin.json` 通用字段

```json
{
  "name": "time",
  "type": "mcp",
  "version": "1.0.0",
  "description": "查询指定时区的当前时间",
  "enabled": true,
  "entry": {}
}
```

`name` 只允许 `A-Z a-z 0-9 _ -`（会进入工具命名空间）；`type` 当前支持
`mcp` / `hook`（未来可扩展 skill / model 等类别）；`enabled: false` 的插件
结构仍会校验但不会加载。字段写错启动即报错。

### MCP 插件（`type: "mcp"`）

```json
{
  "name": "filesystem",
  "type": "mcp",
  "entry": {
    "command": "cmd.exe",
    "args": ["/c", "npx", "-y", "@modelcontextprotocol/server-filesystem", "E:/demo"]
  }
}
```

- `command == "python"` 会自动替换为当前解释器；`args` 中相对路径且真实存在于
  插件目录的文件会被解析为绝对路径（所以写 `"args": ["server.py"]` 即可），
  其余参数（如 `npx -y`）原样保留。
- 子进程工作目录固定为该插件目录，插件自带资源用相对路径即可。
- `transport` 目前只支持 `stdio`（远程 HTTP 在 Roadmap）。

### 钩子插件（`type: "hook"`）

```json
{
  "name": "permission",
  "type": "hook",
  "entry": { "module": "hook.py", "factory": "create_hook" }
}
```

加载器从插件目录动态导入 `module`，调用 `factory(plugin_dir)`，要求返回
`LifecycleHooks` 实例。钩子插件可以读取自己目录下的配置文件（如
`permission.json`），整包拖走即可复用。

生命周期扩展点（`core/hooks.py`）：

| 事件 | 时机 | 说明 |
|---|---|---|
| `turn_start` | 每轮开始时 | 可注入状态 |
| `llm_response` | 模型回复后 | 观察/记录模型输出 |
| `tool_before` | 工具执行前 | 返回 `False` 即拒绝（权限闸门，任一拒绝即拒绝） |
| `tool_after` | 工具执行后 | 观察结果；异常不影响 loop |
| `turn_end` | 每轮结束时 | 收尾 |

## MCP 网关与工具命名空间

- Agent 进程只与 `McpGateway` 一个对象交互：连接、超时、失败清理、退出断开
  全部封装在网关内，loop 不感知任何 MCP 细节；
- 工具以 `<插件名>__<工具名>` 暴露（如 `time__get_current_time`、
  `filesystem__write_file`），多插件同名工具天然隔离；
- 权限策略因此按命名空间匹配：示例见
  `plugins/hooks/permission/permission.json`（如
  `filesystem__delete_file → deny`）；
- 挂载工具 `use_plugin` 本身是普通内核工具，模型调用后网关负责连接与注册；
  重复挂载幂等，不会产生第二个连接。

## 配置说明

### `.env`（密钥与环境，已 gitignore，绝不提交）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 无 | DeepSeek API Key（必填） |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容接口地址 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 模型名 |
| `DEEPSEEK_TIMEOUT` | `60` | 单次模型请求超时（秒） |
| `DEEPSEEK_MAX_RETRIES` | `3` | 模型请求重试次数（仅安全场景） |
| `MCP_CONNECT_TIMEOUT` | `20` | MCP 插件连接超时（秒） |
| `MCP_CALL_TIMEOUT` | `30` | 单次 MCP 工具调用超时（秒） |

### 权限策略（`plugins/hooks/permission/permission.json`）

```json
{
  "default": "allow",
  "rules": [
    { "tool": "filesystem__delete_file", "mode": "deny" },
    { "tool": "filesystem__write_file", "mode": "ask" }
  ]
}
```

`mode` 只能是 `allow / ask / deny`；`tool` 支持通配符（如 `*__shell`、
`filesystem__*`）。被拒绝的工具不会执行，拒绝原因回填给模型。

## 权限与安全模型

- **插件即信任边界**：MCP 插件可以是任意命令（python / node / npx / docker），
  钩子插件是任意 Python 代码；只应安装可信来源的插件；
- **按需挂载**：启动时只读清单、不连接任何服务器；模型通过 `use_plugin`
  触发连接，避免无谓的进程与上下文开销；
- **工具级权限闸门**：loop 在每次工具执行前走 `tool_before`，任一钩子返回
  拒绝则工具不执行；
- **密钥管理**：API Key 只放 `.env`，已被 gitignore 排除，禁止提交；
- **配置即代码审核面**：每个插件（代码/策略/入口）都是独立可 diff 的目录，
  便于 review。

## 测试与代码规范

```powershell
.venv\Scripts\python.exe -m pytest -q    # 单元测试
ruff check .                             # 静态检查
ruff format .                            # 统一格式
```

GitHub Actions（`.github/workflows/ci.yml`）在每次 push / PR 时自动执行
lint + format 检查 + 全部测试。

## 设计原则

1. 内核固定，能力插件化；插件通过类型化接口接入网关，不 fork 核心。
2. 改行为优先加钩子/插件，而不是改 loop。
3. 重要决策记录为 ADR（见 `docs/adr/`），不靠口头约定。
4. 配置外置且随插件走（环境变量/配置文件/插件目录），密钥绝不进入版本库。
5. 可观测性优先：结构化日志、异常隔离、可回放。
6. 代码必须配套测试，测试通过才允许合并。
7. LLM 优先、流程从简：内核只提供最小行动能力，规划与工具组合交给模型本身。

## Roadmap

- 远程 HTTP MCP 插件（`transport: "http"` + URL + 服务器级信任）
- Skills 型插件（按需注入操作说明，plugin.json `type: "skill"`）
- 模型适配器插件化（`type: "model"`，替换 `models/` 固定目录）
- 钩子事件扩展（用户输入提交前、会话开始/结束等，对齐 Codex/Claude Code 拦截点）
- MCP 网关进程化：把 `McpGateway` 换成独立代理进程/远程网关客户端（同一窄接口）
