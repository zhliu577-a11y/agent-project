# Harness Agent

[![CI](https://github.com/zhliu577-a11y/agent-project/actions/workflows/ci.yml/badge.svg)](https://github.com/zhliu577-a11y/agent-project/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.13%2B-3776AB?logo=python&logoColor=white)
![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=111111)
![MCP](https://img.shields.io/badge/MCP-stdio-6E56CF)

一个以**固定 Agent Loop + 插件化能力**为核心的本地 Agent Harness，包含 Python
运行时、插件生命周期、模型/MCP/工具/Hook/记忆网关、HTTP API、CLI 和 React
控制台。

内核只负责调度与边界控制。模型、工具、MCP、Hook、Skill、Session、Memory、
Embedding、事件总线、重试策略和上下文压缩均通过受控插件协议接入。仓库同时提供
网页工作区，用于对话、插件包管理、能力查看、审批和运行时活动追踪。

> 项目仍处于快速演进阶段。插件协议、HTTP API 和前端交互可能继续调整，详细设计
> 以 [Harness README](harness/README.md) 与 [ADR](harness/docs/adr/) 为准。

## 核心能力

### 固定 Loop，能力全部来自插件

- 内核执行稳定的 `模型 -> 工具 -> 回填 -> 结束判断` 循环。
- 模型、工具、MCP、Hook、Skill、Session、Memory 等能力按插件装配。
- 网关隔离内核与插件实现，Agent Loop 不依赖具体 Provider 或进程模型。
- Manifest 在加载代码前完成结构、契约和协议版本校验。
- Runtime 统一管理依赖解析、初始化顺序、失败回滚和逆序关闭。

### 完整的插件平台协议

插件体系围绕四项约束设计：

- **Manifest**：唯一声明入口，支持单能力插件和多功能包。
- **Capability Contract**：按受控 `kind` 校验插件产物类型。
- **Lifecycle**：统一执行 `setup -> start -> ready -> stop`，支持失败回滚。
- **Versioned Protocol**：在代码执行前协商 `apiVersion`、`contract` 和
  `protocolVersion`。

内置插件类型覆盖：

| 领域 | 插件类型 |
| --- | --- |
| 交互能力 | `mcp`、`tool`、`hook`、`skill` |
| 模型能力 | `model`、`model-router` |
| 会话与上下文 | `session`、`context`、`compaction` |
| 长期记忆 | `memory`、`memory-index`、`memory-retriever`、`memory-policy`、`memory-extractor`、`embedding` |
| 运行时可观测性 | `listener`、`event-transport`、`retry-policy` |

一个功能包可以通过 `contributes[]` 同时贡献 Tool、Hook、Skill、MCP 等多种能力，
发现阶段统一展开后再进入受信任的 kind 注册表。

### MCP 按需启动

- Runtime 启动时只发现 MCP Manifest，不同时拉起所有 MCP 进程。
- 高频 MCP 可通过 `mcp.preload` 或 `MCP_PRELOAD` 在启动时预加载。
- 其他 MCP 保持 `idle`，由模型先调用 `use_plugin`，再执行
  `initialize -> tools/list -> tool call`。
- 工具统一以 `<插件名>__<工具名>` 注册，例如
  `filesystem__read_text_file`。
- 连接、重试、恢复、工具命名和进程回收统一由 `McpGateway` 管理。
- 内置 `filesystem` MCP 不依赖 `npx`、npm 缓存或外网，默认只能访问
  `harness/data/workspace`。

### 模型与运行时管理

- 模型适配器来自 `plugins/model/*`。
- `ModelGateway` 支持惰性加载、预热、显式切换、静态路由、fallback 和资源回收。
- 支持通过 API 或前端页面配置 `apiKey`、`baseUrl`、模型名、超时和重试次数。
- 模型失败、取消、超时和重试策略由 Host 统一控制。

### Session、Memory 与 Context 分离

- `Session` 保存完整短期对话历史。
- `Context` 插件决定每次模型请求使用哪些上下文消息。
- `Compaction` 插件负责生成历史压缩方案，提交和回滚由 `SessionGateway` 控制。
- `Memory` 保存跨会话长期事实，写入、检索、排序和自动提取分别插件化。
- 长期记忆支持去重、替代、过期、访问统计和 `scope / owner / agent / tenant`
  身份隔离。
- 上下文召回通过只读端口接入，压缩和上下文策略不能直接修改长期记忆。

### Hook、事件与权限

- Hook 覆盖 `user_prompt_submit`、`turn_start`、`llm_response`、`tool_before`、
  `tool_after` 和 `turn_end`。
- `allow / ask / deny` 权限策略支持通配符和按 Agent 覆盖。
- 事件总线只负责观察和分发，不介入主执行路径。
- 支持进程内异步事件传输、事件日志、时间线和工具指标 Listener。
- 重试策略可以插件化决定“是否重试、等待多久”，实际重试执行仍由 Host 控制。

### 本地网页控制台

`frontend/` 提供 React 控制台，包含：

- **Overview**：Runtime、模型、MCP、插件和审批概况。
- **Plugins**：上传、检查、安装、启停和移除插件包。
- **Capabilities**：查看 Tool、MCP、Model、Skill，并配置 MCP 预加载。
- **Chat**：创建和切换 Session、恢复历史、SSE 流式对话、运行事件和审批。
- **Activity**：按发布者、目标、状态和 Trace 查看脱敏活动记录。

前端使用 React 19、TypeScript、Vite、lucide-react、原生 Fetch 和 SSE，不额外
引入状态管理框架。

## 架构

```text
┌──────────────────────────────────────────────────────────────────────┐
│                         React Console                                │
│  Overview / Plugins / Capabilities / Chat / Activity / Approval      │
└─────────────────────────────────┬────────────────────────────────────┘
                                  │ HTTP + SSE
┌─────────────────────────────────▼────────────────────────────────────┐
│                         FastAPI Controller                           │
│  Runtime lifecycle / plugin packages / sessions / chat / approvals   │
└─────────────────────────────────┬────────────────────────────────────┘
                                  │
┌─────────────────────────────────▼────────────────────────────────────┐
│                          HarnessRuntime                              │
│                                                                      │
│  Fixed Agent Loop                                                    │
│    Model -> Tools -> Tool Results -> Model -> Done                   │
│                                                                      │
│  Gateways                                                            │
│    ModelGateway       ToolRegistry + McpGateway                      │
│    HookGateway        SessionGateway                                 │
│    MemoryGateway      ContextGateway       EventGateway              │
│                                                                      │
│  Runtime Services                                                    │
│    dependency graph / lifecycle / retry / tracing / shared services  │
└─────────────────────────────────┬────────────────────────────────────┘
                                  │ manifests + contracts
┌─────────────────────────────────▼────────────────────────────────────┐
│                         plugins/                                     │
│  tools / mcp / hooks / skills / model / session / memory / context   │
│  embedding / listeners / event transports / retry policies           │
└──────────────────────────────────────────────────────────────────────┘
```

运行链路：

```text
discover plugins
  -> validate manifests
  -> negotiate protocol and contracts
  -> resolve runtime dependency graph
  -> load and instantiate selected capabilities
  -> register gateways and tools
  -> setup -> start -> ready
  -> run agent loop
  -> stop in reverse order
```

## 快速开始

### 环境要求

- Python 3.13+
- Node.js 20+
- npm
- Windows、macOS 或 Linux

### 1. 创建 Python 环境

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r harness\requirements.txt
Copy-Item harness\.env.example harness\.env
```

macOS / Linux：

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r harness/requirements.txt
cp harness/.env.example harness/.env
```

编辑 `harness/.env`，至少配置一个模型 Provider，例如：

```dotenv
AGENT_MODEL=deepseek
DEEPSEEK_API_KEY=sk-your-key
```

API Key 只放在本地 `.env`，该文件已被 `.gitignore` 排除。

### 2. 启动 HTTP API

请在普通 PowerShell 或系统终端中运行。MCP 使用 stdio 子进程通信；受限沙箱或
后台任务可能禁止创建子进程管道，从而在 Windows 上出现
`[WinError 5] 拒绝访问`。

```powershell
cd harness
..\.venv\Scripts\python.exe -m api
```

默认地址：

- API：`http://127.0.0.1:8000`
- OpenAPI：`http://127.0.0.1:8000/docs`

### 3. 启动网页控制台

另开一个终端：

```powershell
cd frontend
npm install
npm run dev
```

访问 `http://127.0.0.1:5173`。Vite 会把 `/api` 代理到
`http://127.0.0.1:8000`。

### 4. 使用 CLI

交互式 Agent：

```powershell
cd harness
..\.venv\Scripts\python.exe main.py
```

插件包管理：

```powershell
..\.venv\Scripts\python.exe cli.py plugin list
..\.venv\Scripts\python.exe cli.py plugin validate path\to\plugin.zip
..\.venv\Scripts\python.exe cli.py plugin install path\to\plugin.zip
..\.venv\Scripts\python.exe cli.py plugin enable plugin-name
```

MCP 预加载管理：

```powershell
..\.venv\Scripts\python.exe cli.py mcp list
..\.venv\Scripts\python.exe cli.py mcp preload add filesystem
..\.venv\Scripts\python.exe cli.py mcp preload remove filesystem
```

## 项目结构

```text
.
├── harness/                     # Python Agent Harness
│   ├── api/                     # FastAPI 与控制服务
│   ├── core/                    # 类型、协议、工具、事件、重试和状态
│   ├── gateways/                # Model、MCP、Hook、Session、Memory 等网关
│   ├── plugins/                 # 内置插件与插件平台实现
│   │   ├── mcp/                 # MCP 插件
│   │   ├── tools/               # 进程内工具插件
│   │   ├── hooks/               # 生命周期和权限 Hook
│   │   ├── skills/              # 渐进披露 Skill
│   │   ├── model/               # 模型适配器
│   │   ├── session/             # 短期会话存储与压缩
│   │   ├── memory/              # 长期记忆 Store
│   │   ├── memory-retriever/    # 召回策略
│   │   ├── memory-policy/       # 写入与排序策略
│   │   ├── context/             # 模型上下文策略
│   │   └── ...
│   ├── config/                  # 随仓库提交的中心配置
│   ├── docs/                    # API 契约与 ADR
│   ├── tests/                   # 后端单元与集成测试
│   ├── loop.py                  # 固定 Agent Loop
│   ├── runtime.py               # Runtime 对象图与生命周期
│   ├── cli.py                   # 插件和 Skill/MCP 管理 CLI
│   └── main.py                  # 交互式 Agent 入口
├── frontend/                    # React 控制台
│   ├── src/api/                 # API 客户端与类型
│   ├── src/views/               # Overview、Plugins、Chat 等页面
│   ├── src/components/          # 公共 UI 组件
│   └── scripts/verify-ui.mjs    # Playwright UI 验证
└── .github/workflows/ci.yml     # 后端 lint、格式和测试 CI
```

运行时生成的数据默认放在 `harness/.runtime/`、`harness/data/`、
`harness/.sessions/` 和 `harness/.memory/`，均不应提交到仓库。

## 配置

配置分为三层：

```text
harness/config/config.json                 # 共享默认值
harness/config/<section>.json              # model / session / memory / ...
harness/config/plugins/<kind>/<name>.json  # 单插件私有配置
harness/.env                               # API Key 与本机覆盖，不提交
```

优先级：

```text
环境变量 > 分域/单插件配置 > config/config.json > 内置默认
```

常见环境变量：

| 变量 | 说明 |
| --- | --- |
| `AGENT_MODEL` | 激活的模型插件 |
| `SESSION_STORE` | `jsonl` 或 `inmemory` |
| `SESSION_COMPACTION` | 会话压缩策略 |
| `MEMORY_STORE` | `sqlite`、`jsonl` 或 `vector` |
| `MEMORY_RETRIEVER` | 长期记忆召回策略 |
| `CONTEXT_STRATEGY` | 模型上下文策略 |
| `EVENT_TRANSPORT` | 事件传输插件 |
| `RETRY_POLICY` | Host 采用的重试决策策略 |
| `MCP_PRELOAD` | 启动时预加载的 MCP，逗号分隔 |
| `MCP_CONNECT_TIMEOUT` | MCP 连接超时 |
| `MCP_CALL_TIMEOUT` | MCP 工具调用超时 |

完整配置说明见 [Harness README](harness/README.md#配置说明)。

## 测试与校验

后端：

```powershell
cd harness
..\.venv\Scripts\python.exe -m pytest -q
..\.venv\Scripts\python.exe -m ruff check .
..\.venv\Scripts\python.exe -m ruff format --check .
```

前端：

```powershell
cd frontend
npm run typecheck
npm run build
npm run verify:ui
```

`verify:ui` 需要后端和 Vite 开发服务器保持运行。

CI 在 [ci.yml](.github/workflows/ci.yml) 中执行 Python lint、format check 和测试。

## 安全边界

- 插件代码和 MCP 命令属于可信执行边界，只安装可信来源的插件。
- 插件包安装会检查 zip 路径穿越、符号链接、包大小和入口逃逸，但这不是沙箱。
- 工具调用在执行前经过 HookGateway，可配置 `allow / ask / deny`。
- 内置 `filesystem` 仅允许访问显式配置的根目录，并拒绝 `..`、绝对路径逃逸和
  符号链接逃逸。
- API Key 仅存放于 `.env` 或本地数据文件，不通过 API 返回明文。
- 事件日志对 `key`、`token`、`password`、`secret` 和 `auth` 等字段脱敏。

## 文档

- [完整 Harness 文档](harness/README.md)
- [HTTP API 契约](harness/docs/api.md)
- [插件 Manifest Schema](harness/docs/plugin-manifest.schema.json)
- [架构决策记录 ADR](harness/docs/adr/)
- [前端控制台说明](frontend/README.md)

## Roadmap

- 插件包更新、版本替换与运行时热重载
- 远程 HTTP MCP 与独立 MCP Gateway 进程
- 多 Agent 依赖、能力图、权限和记忆隔离
- Skill 资源分发、触发评估与更多兼容格式
- 更完整的恢复策略与可观测性
- 前端插件市场与拖拽安装工作流
