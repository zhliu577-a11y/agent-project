# agent-project

一个“固定 Harness + 一切皆插件”的精简 agent：**内核（loop）只做编排，
能力全部来自插件目录，插件通过网关接入**。

本仓库借鉴 Harness 工程（Claude Code / DeepSeek Harness 一脉）的组织方式：
生命周期行为做成 **hooks 插件**，外部工具做成 **MCP 插件**；内核不直接接触
任何插件实现，只面向两个网关——`HookGateway`（钩子网关）与 `McpGateway`
（MCP 网关）。模型适配器同样是插件（`plugins/model/*`），由 `AGENT_MODEL`
选定。

## 特性

- 固定异步 agent loop（调模型 → 执行工具 → 回填 → 判断结束），支持流式输出与多个工具的并行执行
- **插件目录（drop-in）**：`plugins/` 下每个插件是一个自包含目录 + `plugin.json`，
  拖入即可被 Agent 发现；目前支持 `mcp`、`hook`、`tool`、`model`、`model-router`、
  `skill`、`session`、`memory`、`embedding`、`listener`、`context` 十一类
- **功能包**：一个包可用 `contributes[]` 同时提供 skill、hook、tool 等能力，
  发现阶段统一展开为 contribution 后按受控 kind 注册表装配
- **MCP 网关**：Agent 只面向网关这一个通道；网关统一维护各 MCP 插件的连接、
  会话、命名与清理，工具名带命名空间（`<插件名>__<工具名>`）
- **钩子网关**：生命周期钩子全部插件化（`turn_start / llm_response /
  tool_before / tool_after / turn_end`），内核只面向 `HookGateway`
- **本地工具插件**：高频轻量能力以进程内 Python 函数提供（`plugins/tools/*`），
  启动即注册，无子进程、无挂载步骤
- **外部工具运行时**：`tool` 插件也可声明 `runtime: node | process` 和
  `protocol: jsonrpc-stdio`，用 TypeScript/Node 或其它语言实现；进程首次调用时
  启动，Runtime 关闭时统一回收
- **模型插件**：LLM 适配器来自 `plugins/model/*`（当前 deepseek 为默认）；
  `ModelGateway` 统一负责惰性加载、预热、路由、fallback 与资源回收，
  agent loop 始终只依赖 `ModelAdapter.complete()`
- **技能插件**：纯内容插件（`plugins/skills/*`）按需注入操作说明——启动只放
  目录条目，模型需要时用 `use_skill` 读取完整正文（渐进披露）
- **会话插件**：短期记忆插件化（`plugins/session/*`）——同一会话多轮历史
  自动延续，退出后按 `SESSION_ID` 恢复
- **长期记忆插件**：跨会话语义笔记（`plugins/memory/*`），模型通过
  `remember / recall / forget` 主动读写
- **上下文策略插件**：完整历史由 Session 保存，模型请求前由可替换的
  `context` 插件生成上下文视图；`CONTEXT_STRATEGY` 可切换策略
- **插件包生命周期**：外部目录或 zip 包经过静态检查、staging、摘要计算后原子安装；
  安装后默认禁用，启停状态由 `data/plugin-registry.json` 管理
- **HarnessRuntime**：统一装配插件、网关、模型、会话和工具；启动成功、加载失败
  与关闭状态会回写到 registry，CLI 和未来网页只依赖这一运行时边界
- **CLI**：`python cli.py plugin ...` 提供 validate / install / enable / disable /
  remove / list；后续网页前端复用同一套 `PluginManager`
- **MCP 预加载**：宿主通过 `mcp.preload` 或 `MCP_PRELOAD` 选择启动即挂载的
  MCP；其余 MCP 仍由模型通过 `use_plugin` 按需挂载
- 权限钩子插件示例：`allow / ask / deny` 策略随插件文件夹走，支持通配符
- 配置文件带 schema 校验：写错清单/策略启动即报错，绝不静默出错
- 单元测试 + ruff 规范 + GitHub Actions CI

## 架构

```text
┌──────────────────────── 内核（固定，不随插件变化）───────────────────────┐
│  loop.py       固定循环：调模型 → 工具 → 回填                           │
│  runtime.py    Runtime：生命周期 roots → 装配对象图 → 关闭与状态回写    │
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
│  permission / audit  │   │  plugins/tools/*（text…，启动即注册）        │
└──────────────────────┘   └────────────────────────────────────────────┘
```

运行链路：启动时扫描 `plugins/` 与 registry 中已启用的外部包 →
钩子插件实例化进 `HookGateway`、本地工具
直接注册进 `ToolRegistry`、MCP 插件只读清单，并按 `mcp.preload` 预挂载选中项 →
本地工具和预加载的 MCP 工具第一轮即可调用；其余 MCP 工具由模型决定调
`use_plugin` → 网关连接对应 MCP 插件并注册命名空间工具（都先过权限钩子闸门）
→ 结果回填 → 模型给出最终回答。

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
├── tools/                      # 工具插件（Python 进程内或外部进程）
│   ├── text/                   #   文本工具示例（slugify / count_words）
│   └── json/                   #   JSON 处理（format / get）
│       ├── plugin.json         #   默认 runtime=python
│       └── tool.py             #   实现 Tool 的类 + create_tools 工厂
├── model/                      # 模型适配器插件
│   ├── deepseek/               #   DeepSeek（默认）
│   │   ├── plugin.json
│   │   ├── model.py            #   ModelAdapter 实现 + create_model 工厂
│   │   └── README.md
│   └── openai/                 #   OpenAI（示例：如何加第二家模型）
│       ├── plugin.json
│       └── model.py
├── skills/                     # 技能插件（纯内容，不执行代码）
│   ├── code-review/            #   代码评审规范
│   └── commit-message/         #   Git 提交信息规范
│       ├── plugin.json         #   { "type": "skill", ... }
│       └── SKILL.md            #   模型按需读取的完整说明
├── session/                    # 会话存储插件（短期记忆）
│   ├── jsonl/                  #   文件持久化（默认，重启可恢复）
│   └── inmemory/               #   纯内存（重启即失，测试/临时用）
│       ├── plugin.json         #   { "type": "session", ... }
│       └── store.py            #   SessionStore 实现
├── memory/                     # 长期记忆插件（跨会话语义笔记）
│   ├── sqlite/                 #   生产默认：事务 + WAL + 参数化查询
│   ├── jsonl/                  #   人眼可读的示例后端
│   └── vector/                 #   语义检索后端（存 embedding + 余弦排序）
│       ├── plugin.json         #   { "type": "memory", ... }
│       └── store.py            #   MemoryStore 实现
├── embedding/                  # 嵌入提供方插件（向量记忆后端使用）
│   ├── debug/                  #   确定性哈希（离线开发/测试）
│   └── openai-embedding/       #   OpenAI API（真实语义，需 OPENAI_API_KEY）
├── context/                    # 上下文策略插件（可切换模型请求视图）
│   ├── tail-window/            #   默认：保留 system 与最新完整消息组
│   └── summary-window/         #   旧消息抽取摘要 + 保留最新完整消息组
├── listeners/                  # 事件订阅者插件（接入事件总线）
│   └── timeline/               #   把事件写成 JSONL 时间线（示例）
└── hooks/                      # 生命周期钩子插件
    └── permission/             #   权限策略示例（allow/ask/deny）
        ├── plugin.json
        ├── hook.py             #   LifecycleHooks 实现 + create_hook 工厂
        └── permission.json     #   策略文件随插件走
```

### `plugin.json` 通用字段

```json
{
  "apiVersion": "1",
  "name": "time",
  "type": "mcp",
  "version": "1.0.0",
  "protocolVersion": 1,
  "contract": "mcp.v1",
  "description": "查询指定时区的当前时间",
  "enabled": true,
  "entry": {}
}
```

`name` 只允许 `A-Z a-z 0-9 _ -`（会进入工具命名空间）；`type` 当前支持
`mcp` / `hook` / `tool` / `model` / `skill` / `session` / `memory` / `embedding` /
`listener` / `context`；
`apiVersion` 固定为 `"1"`，`protocolVersion` 当前为 `1`，`contract` 必须与
`<type>.v<protocolVersion>` 一致（例如 `mcp.v1`、`tool.v1`）；
`enabled: false` 的插件
结构仍会校验但不会加载。可选字段 `priority`（整数，默认 `0`）决定钩子插件的
执行顺序：**越小越先执行**；可选字段 `errors` 声明插件已知的领域错误
（见下文“错误分类与重试”）。字段写错启动即报错。

### 功能包（`contributes[]`）

需要把多个能力一起发布、启停和版本化时，可以让一个目录提供功能包清单：

```json
{
  "apiVersion": "1",
  "name": "code-quality",
  "version": "1.0.0",
  "protocolVersion": 1,
  "contributes": [
    {
      "id": "lint",
      "kind": "skill",
      "contract": "skill.v1",
      "entry": { "content": "skills/lint/SKILL.md" }
    },
    {
      "id": "format-hook",
      "kind": "hook",
      "contract": "hook.v1",
      "entry": { "module": "hooks/hook.py", "factory": "create_hook" }
    }
  ]
}
```

包内路径统一相对包根目录解析；`enabled`、`priority`、`errors` 可在包级设置，
并在 contribution 级覆盖或追加。每个 contribution 会展开为
`<包名>--<contribution-id>`，例如 `code-quality--lint`。`kind` 必须来自
受信任核心代码注册的 kind；插件清单不能自行注册新的执行阶段。

### 安装、启停与生命周期

`plugin.json` 只描述插件包，不保存宿主的安装状态。外部插件包通过 CLI 安装：

```powershell
.venv\Scripts\python.exe -m cli plugin validate .\code-quality.zip
.venv\Scripts\python.exe -m cli plugin install .\code-quality.zip
.venv\Scripts\python.exe -m cli plugin enable code-quality
.venv\Scripts\python.exe -m cli plugin list
```

宿主状态保存在 `data/plugin-registry.json`，包文件保存在
`data/plugin-store/installed/<name>/<version>/`。安装流程为：

```text
validate -> staging -> 静态入口检查 -> sha256 -> 原子移动 -> registry
```

安装后默认 `disabled`；`enable / disable` 只修改 registry，不重写插件自带的
`plugin.json`。第一版不做热加载，启停后需要重启或重建 Runtime 才生效。
`remove` 先把目录移动到 `data/plugin-store/trash/`，便于后续审计或恢复。

`runtimeStatus` 记录最近一次 Runtime 装配结果：

| 状态 | 含义 |
|---|---|
| `disabled` | 用户已禁用，本次 Runtime 不装配 |
| `idle` | 已启用，但当前尚未完成装配 |
| `active` | 当前 Runtime 已成功装配 |
| `error` | 当前包的声明、入口或初始化失败，错误写入 `lastError` |
| `stopped` | 上次 Runtime 正常关闭 |

外部插件按包隔离装配：一个包失败只把它标为 `error`，不会自动把其它插件标错。
如果失败包恰好提供当前选中的 model/session/memory，Runtime 仍会拒绝启动。
`python cli.py plugin list` 会同时显示期望启停状态和最近一次运行状态。

当前只接受目录和 zip。zip 解压会拒绝路径穿越和符号链接，并限制文件数量与
总大小；入口路径也不能逃出插件包根目录。插件代码仍属于可信执行边界，这些
检查不是沙箱，也不会自动安装插件依赖。

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

MCP 默认按需挂载：Runtime 启动时只读取插件清单，模型调用 `use_plugin` 后
才连接服务器并执行 `tools/list`。频繁使用、希望第一轮模型请求就携带工具
schema 的插件，可以显式预加载：

```json
{
  "mcp": {
    "preload": ["time", "math"]
  }
}
```

也可用逗号分隔的环境变量覆盖配置文件：

```powershell
$env:MCP_PRELOAD = "time,math"
```

CLI 管理命令：

```powershell
python cli.py mcp list
python cli.py mcp preload add time
python cli.py mcp preload remove time
```

预加载会增加启动时间和常驻子进程数量，建议只放高频 MCP。连接失败不会阻止
Runtime 启动，后续仍可调用 `use_plugin` 重试；配置了不存在的名称会在启动时
明确报错，避免拼写错误被静默忽略。

### 本地工具插件（`type: "tool"`）

```json
{
  "name": "text",
  "type": "tool",
  "entry": { "module": "tool.py", "factory": "create_tools" }
}
```

`tool.py` 实现 `core.tool.Tool`，工厂 `create_tools(plugin_dir)` 返回单个 Tool
或 Tool 列表；加载器自动把每个工具包装成 `<插件名>__<工具名>`
（如 `text__slugify`）并在启动时注册，模型第一轮就能调用。完整示例见
`plugins/tools/text/`。

什么时候用本地 tool、什么时候用 mcp：

| 场景 | 用 `tool` | 用 `mcp` |
|---|---|---|
| 高频轻量、纯函数、内部逻辑 | Python 进程内（零成本） | 偏重 |
| TypeScript/Node 生态、需要独立进程 | 外部 `runtime` 工具 | 也可以 |
| 已有 npx / CLI / docker 能力且只需内部使用 | 外部 `runtime` 工具 | 也可以 |
| 将来要让别的 MCP 客户端复用 | 否 | 是 |

这些工具在模型侧无差别：都是 `<插件名>__<工具名>`，都过同一套权限/审计钩子；
区别只在“能力怎么托管”。

### 外部进程工具插件（Node / TypeScript 等）

不改变现有的 Python 工具插件写法；需要 TypeScript/Node 或独立进程时，在
`entry` 中改用 `runtime` + `protocol`：

```json
{
  "name": "browser-lite",
  "type": "tool",
  "entry": {
    "runtime": "node",
    "protocol": "jsonrpc-stdio",
    "command": "node",
    "args": ["dist/index.js"],
    "timeout": 30,
    "tools": [
      {
        "name": "open_url",
        "description": "Open a URL and return the page title.",
        "parameters": {
          "type": "object",
          "properties": { "url": { "type": "string" } },
          "required": ["url"]
        }
      }
    ]
  }
}
```

- `runtime` 支持 `node` 和 `process`；`process` 用于其它语言，实际执行命令由
  `command` 决定。`command` 可以是字符串 + `args`，也可以直接写成
  `["node", "dist/index.js"]`。
- `protocol` 目前只支持 `jsonrpc-stdio`。进程工作目录固定为插件目录；命令中
  指向插件目录内真实文件的相对参数会自动转成绝对路径。
- `entry.tools` 是静态工具目录，启动装配时注册 schema，不需要先启动进程。
  进程在第一次调用时惰性启动，同一插件的工具共享一个进程。
- 子进程的 `stdout` 必须只输出一行一个 JSON-RPC 响应；普通日志写
  `stderr`，宿主会带插件名转发到日志。
- Runtime 关闭时会 terminate 外部进程。当前一个插件进程内的调用按顺序执行。

对应 TypeScript 侧只需要处理 `tools/call`：

```typescript
import * as readline from "node:readline";

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", (line) => {
  const request = JSON.parse(line);
  if (request.method !== "tools/call") return;

  const { name, arguments: args } = request.params;
  const result =
    name === "open_url"
      ? { content: [{ type: "text", text: `opened ${args.url}` }] }
      : { content: [{ type: "text", text: `unknown tool: ${name}` }] };

  process.stdout.write(
    JSON.stringify({ jsonrpc: "2.0", id: request.id, result }) + "\n",
  );
});
```

JSON-RPC `error.code` 是字符串且命中该插件 `errors` 声明时，会按统一规则翻译成
`DeclaredPluginError`；其它错误会成为普通 `ToolError`。协议同一时刻只处理一个
请求，宿主会对同一插件的调用做串行化。

### 模型插件（`type: "model"`）

```json
{
  "name": "deepseek",
  "type": "model",
  "entry": { "module": "model.py", "factory": "create_model" }
}
```

`model.py` 实现 `core.model.ModelAdapter`，工厂 `create_model(plugin_dir)` 返回
适配器实例。装配阶段只校验入口、**不实例化**（构造可能读取密钥等环境变量），
由 `ModelGateway` 根据默认模型、显式覆盖、agent role 和 fallback 选择提供方后
惰性创建。仓库自带两个示例：`deepseek`（默认）与 `openai`。换模型 = 复制
`plugins/model/openai/` 目录改成自己的适配器，或设置 `AGENT_MODEL=openai`
并使用对应配置。

`Runtime.model` 暴露的是 `ModelGateway` 这一单一 `ModelAdapter`，`Runtime.models`
提供模型资源管理接口：

```python
await runtime.models.use("openai")  # 显式切换到 openai
await runtime.models.prewarm("openai")  # 只初始化，不切换
await runtime.models.unload("openai")  # 等待在途请求结束后释放
runtime.models.status()  # idle / loading / active / error / stopped
```

默认模型与 `keepWarm` 中的 provider 会在 Runtime 启动时预热；其它 provider
保持惰性创建。一次请求结束后 provider 默认继续保留，避免每轮重复建连；
Runtime 关闭时统一调用其可选 `stop()` 并关闭客户端。

模型选择由受信任的 `model-router.v1` 插件执行，默认
[`static`](plugins/model_routers/static/router.py) 规则为：

```text
显式 use 覆盖 -> agent role route -> 默认模型 -> fallback
```

`config/model.json` 示例：

```json
{
  "model": "deepseek",
  "router": "static",
  "fallback": ["openai"],
  "keepWarm": ["deepseek"],
  "routes": {
    "coding": ["deepseek", "openai"],
    "summary": ["openai"]
  }
}
```

fallback 只会在尚未向调用方输出 token 时发生。一旦流式输出已经开始，
Gateway 会直接报错而不会切换模型，避免把两个模型的文本拼到同一条回复里。
provider 的能力元数据（roles、capabilities、contextWindow、tools、streaming、
cost/latency tier）来自各自的 `plugin.json`，路由插件可以据此做更复杂的选择。

内置 provider 优先读取
`config/plugins/model/<name>.json` 的 `apiKey`、`baseUrl`、`model`、`timeout`、
`maxRetries`，环境变量作为兼容 fallback。

Codex 的 `wire_api = "responses"` 配置不能直接塞进现有 OpenAI
`chat.completions` 插件，应使用 `sub2api` 插件。它的非敏感配置位于
`config/plugins/model/sub2api.json`，API key 只放环境变量：

```dotenv
AGENT_MODEL=sub2api
SUB2API_API_KEY=sk-...
SUB2API_BASE_URL=http://172.16.3.6:8589/v1
SUB2API_MODEL=deepseek-v4-flash
SUB2API_DISABLE_RESPONSE_STORAGE=true
```

该插件调用 `client.responses.create(...)`，把内部消息转换为 Responses
input items，并把 `response.output_text.delta` 与 function-call 事件解析回
`ModelResponse`。它支持流式文本和工具调用；`store=false` 对应 Codex 配置中的
`disable_response_storage = true`。

模型插件只负责**传输**（鉴权 / base_url / 请求体 / 取流）；把流式分片翻译成
`ModelResponse` 的解析逻辑由内核 [core/parser.py] 提供（`ResponseParser` +
`ToolCallAccumulator` + `OpenAICompatibleParser`），deepseek/openai 直接复用，
新接 OpenAI 兼容 provider 不必再抄一遍解析代码。

### 技能插件（`type: "skill"`）

```json
{
  "name": "code-review",
  "type": "skill",
  "entry": {
    "content": "SKILL.md",
    "resources": [
      { "path": "references/security.md", "description": "安全检查参考" }
    ]
  }
}
```

技能是**纯内容插件**：不执行代码、不注册工具，只是一份 Markdown 操作说明
和可选附属资料。
渐进披露规则：

- 启动时系统提示词里只有“目录条目”（技能名 + 一句话描述）；
- 模型需要某技能时调用 `use_skill(name)`，网关才读取该插件正文并回填
  （惰性读取 + 缓存），`use_skill` 同样过权限/审计钩子；
- 附属资源默认只展示清单，模型通过 `use_skill(name, resource=path)` 按需读取；
- 正文、资源和预载总量都有字节预算，超限明确失败，不做静默截断；
- 全局规则类技能只能由宿主在 `config/skill.json` 的 `preload` 列表中选择，
  旧清单字段 `entry.preload` 会被忽略。

读取成功会发布 `skill.loaded` / `skill.resource_loaded`，启动预载发布
`skill.preloaded`；`RuntimeSnapshot.skills` 暴露 `status / preload / loaded /
bytes / error`。

完整示例见 `plugins/skills/code-review/`。

### 会话存储插件（`type: "session"`）

```json
{
  "name": "jsonl",
  "type": "session",
  "entry": { "module": "store.py", "factory": "create_store" }
}
```

会话插件实现 `core.session.SessionStore`（`load` / `save`），是短期记忆的
存储后端：

- `SESSION_STORE`（默认 `jsonl`）选择激活的存储插件，语义与 `AGENT_MODEL` 一致；
- 同一会话内每轮历史自动延续（loop 的 `history` 参数），退出后同
  `SESSION_ID`（默认 `default`）自动恢复；
- jsonl 示例把消息写在 `SESSION_DATA_DIR`（默认项目下 `.sessions/`，
  已 gitignore）——换存储 = 复制 `plugins/session/jsonl/` 改实现。

### 长期记忆插件（`type: "memory"`）

```json
{
  "name": "jsonl",
  "type": "memory",
  "entry": { "module": "store.py", "factory": "create_store" }
}
```

实现 `core.memory.MemoryStore`（`add_note` / `search_notes` / `delete_note`）。
默认后端是 **sqlite**（生产方向：事务 + WAL + busy_timeout + 参数化 SQL，
数据文件 `MEMORY_DB_PATH`，默认 `.memory/memory.db`）；`jsonl` 作为人眼可读的
示例后端（`.memory/memory.jsonl`）。换后端只改 `MEMORY_STORE`。

**语义检索后端**：`MEMORY_STORE=vector` 时，笔记会额外保存 embedding 向量，
`recall` 先把 query 转成向量、按余弦相似度取 Top-K；相似度低于阈值自动回退
子串/标签搜索。嵌入来源由 `EMBEDDING_PROVIDER` 选择：`debug`（确定性哈希，
离线跑通链路）或 `openai-embedding`（真实语义，需 `OPENAI_API_KEY`）。
模型侧提供三个工具：

- `remember(content, tags?)`：把跨会话值得记住的事实写成笔记；
- `recall(query?)`：按内容/标签检索（当前为子串/标签匹配）并返回笔记；
- `update_note(note_id, content?, tags?)`：更新已有笔记（字段可省略）；
- `forget(note_id)`：删除过期笔记。

它们和普通工具一样过权限/审计钩子。

### 上下文策略插件（`type: "context"`）

```json
{
  "name": "tail-window",
  "type": "context",
  "entry": { "module": "policy.py", "factory": "create_policy" }
}
```

上下文策略实现 `core.context.ContextPolicy`：

```python
async def prepare(request: ContextRequest) -> ContextResult: ...
```

- `CONTEXT_STRATEGY`（默认 `tail-window`）选择当前策略；
- 策略在每次模型调用前执行，包括同一个 agent turn 内的工具循环；
- `ContextRequest` 提供完整消息、工具 schema、token 预算、会话与 turn 状态；
- `ContextResult` 只作为“本次发给模型的视图”，不会覆盖 Session 中的完整历史；
- 默认 `tail-window` 会计算 system prompt 与工具 schema，保留最新完整消息组，
  不会拆开 `assistant(tool_calls)` 与对应的 `tool` 结果；
- `summary-window` 额外预留 25% 上下文预算，把较早消息抽取成有限长度摘要并
  并入 system prompt，再原样保留最新完整消息组；
- 策略异常、返回非法 tool-call 序列或仍超预算时，loop 回退到 `tail-window`，
  保证模型调用边界仍可用。

`CONTEXT_MAX_TOKENS`（默认 20000）是该视图的估算预算。摘要类策略必须在
插件内部使用独立预算，不能递归进入完整 agent loop。

切换内置策略：

```powershell
$env:CONTEXT_STRATEGY="summary-window"
```

### 错误分类与重试（边界翻译）

外部异常（openai / sqlite / 子进程 / 文件）不会自动带语义，因此在四个边界
统一翻译成内部类别：

- `core/errors.py`：`classify_error()`（白名单映射，不认识的原样透传；
  `CancelledError` 等控制异常绝不吞）、`translate_error()`（带上下文并保留
  `__cause__`）、`boundary()` 装饰器；
- 映射规则：超时 / 连接错误 / 408 / 429 / 5xx → `RetryableError`；
  其它 HTTP 状态 → `ModelError`；`sqlite3.Error` → `ToolError`；
  `OSError` → `PluginError`；
- 消费方：`retry_async`（重试判定）和 loop（工具失败反馈会带上错误类别，
  瞬时错误提示模型可稍后重试）。

**插件错误契约（声明式）**：插件可在 `plugin.json` 里声明已知错误，让调用方
（尤其是模型）知道“会报什么、该怎么办”：

```json
{
  "errors": [
    { "code": "invalid_json", "category": "tool", "hint": "检查 JSON 语法后重试" },
    { "code": "rate_limited", "category": "retryable", "hint": "稍后重试" }
  ]
}
```

- `code` 唯一、`category` 必须来自内核固定集合、`retryable` 需与 category 自洽；
- 进程内插件抛 `DeclaredPluginError(code, message)`，由包装层按声明补全；
- 外部进程工具返回 JSON-RPC `error.code` 字符串时，按同一份声明补全；MCP 和
  外部工具都把未声明的错误退化为普通 `ToolError`；
- MCP 插件返回 `isError` 且文本以 `[code] …` 或 `code: …` 开头时按声明匹配，
  匹配不到则退化为通用 `ToolError`；
- 工具失败回填给模型时会带上 `错误类别 + code + hint`，例如
  `错误类别: retryable，code: rate_limited（瞬时错误，可稍后重试）`。

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
| `tool_before` | 工具执行前 | 表态 `allow / ask / deny`（权限闸门） |
| `tool_after` | 工具执行后 | 观察结果；异常不影响 loop |
| `turn_end` | 每轮结束时 | 收尾 |

钩子执行顺序与决策折叠（`HookGateway`）：

- 全部钩子按 `(priority, 注册顺序)` 升序执行，同优先级保持加入次序，稳定可预测；
  权限/安全类闸门建议用较小的 `priority`（先表态）；
- `tool_before` 让**所有**钩子表态后按 `deny > ask > allow` 折叠，不做“首个拒绝
  即短路”，这样审计类钩子也能看到被拒的尝试；
- 折叠结果是 `ask` 时，网关只向用户确认一次（不会因多个钩子要 ask 而重复弹窗）；
- 钩子抛异常视为该钩子表态 `deny`（安全侧默认拒绝），但不阻断其余钩子表态。

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

### `config/`（中心配置，随仓库提交）

`config/config.json` 承载共享默认值，`config/<section>.json` 承载模型、会话、
记忆、嵌入、上下文、MCP 和 Skill 等分域配置，`config/plugins/<kind>/<name>.json`
承载单个插件的私有配置。优先级为
**环境变量 > 分域/单插件配置 > config/config.json > 内置默认**；
密钥与数据目录仍只放 `.env`。

```json
{
  "model": "deepseek",
  "session": { "store": "jsonl", "id": "default" },
  "memory": { "store": "sqlite" },
  "embedding": { "provider": "debug" },
  "context": { "maxTokens": 20000, "strategy": "tail-window" }
}
```

对应字段写错启动即报错（根目录 `config.py` 校验）；想临时换后端，用环境变量
覆盖即可（例如 `SESSION_STORE=inmemory`）。MCP 预加载列表由
`config/mcp.json` 管理，CLI 的 `mcp preload add/remove` 会更新该文件。
Skill 的宿主预载和内容预算由 `config/skill.json` 管理。

### `.env`（密钥与环境，已 gitignore，绝不提交）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 无 | DeepSeek API Key（必填） |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容接口地址 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 模型名 |
| `DEEPSEEK_TIMEOUT` | `60` | 单次模型请求超时（秒） |
| `DEEPSEEK_MAX_RETRIES` | `3` | 模型请求重试次数（仅安全场景） |
| `AGENT_MODEL` | `deepseek` | 激活的模型插件名（plugins/model/* 里选） |
| `MODEL_FALLBACK` | 无 | fallback 模型名，逗号分隔；覆盖 `model.fallback` |
| `MODEL_KEEP_WARM` | 无 | Runtime 启动时预初始化的模型名，逗号分隔 |
| `MODEL_ROUTER` | 无 | 激活的路由插件名；未设置时使用 Gateway 内置默认路由 |
| `SUB2API_API_KEY` | 无 | Sub2API Responses 网关 token |
| `SUB2API_BASE_URL` | 插件配置 | Sub2API 的 `/v1` 地址 |
| `SUB2API_MODEL` | `deepseek-v4-flash` | Responses 网关使用的模型名 |
| `SUB2API_DISABLE_RESPONSE_STORAGE` | `true` | 是否在网关禁用响应存储 |
| `SESSION_STORE` | `jsonl` | 激活的会话存储插件名（plugins/session/* 里选） |
| `SESSION_ID` | `default` | 会话标识，同名会话自动恢复历史 |
| `SESSION_DATA_DIR` | `.sessions/` | jsonl 会话数据目录 |
| `MEMORY_STORE` | `sqlite` | 激活的长期记忆插件名（plugins/memory/* 里选） |
| `MEMORY_DB_PATH` | `.memory/memory.db` | sqlite 后端的数据文件路径 |
| `MEMORY_DATA_DIR` | `.memory/` | jsonl 长期记忆数据目录 |
| `MEMORY_VECTOR_DB_PATH` | `.memory/vector.db` | vector 后端数据文件 |
| `EMBEDDING_PROVIDER` | `debug` | 嵌入提供方插件（plugins/embedding/* 里选） |
| `VECTOR_SIMILARITY_THRESHOLD` | `0.2` | 低于该相似度则回退字面搜索 |
| `VECTOR_TOP_K` | `3` | 语义检索最多返回条数 |
| `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small` | OpenAI 嵌入模型名 |
| `CONTEXT_MAX_TOKENS` | `20000` | 单次模型请求上下文的估算预算 |
| `CONTEXT_STRATEGY` | `tail-window` | 激活的上下文策略插件（plugins/context/* 里选） |
| `MCP_CONNECT_TIMEOUT` | `20` | MCP 插件连接超时（秒） |
| `MCP_CALL_TIMEOUT` | `30` | 单次 MCP 工具调用超时（秒） |
| `MCP_CONNECT_RETRIES` | `0` | MCP 连接失败额外重试次数（指数退避，默认不重试） |
| `MCP_PRELOAD` | 无 | Runtime 启动时预加载的 MCP 插件名，逗号分隔；覆盖 `mcp.preload` |
| `SKILL_PRELOAD` | 无 | Runtime 启动时注入系统提示词的 Skill 名，逗号分隔；覆盖 `skill.preload` |
| `SKILL_MAX_CONTENT_BYTES` | `262144` | 单个 Skill 正文最大 UTF-8 字节数 |
| `SKILL_MAX_PRELOAD_BYTES` | `524288` | 所有预载 Skill 正文合计最大字节数 |
| `SKILL_MAX_RESOURCES` | `32` | 单个 Skill 最多声明的资源数 |
| `SKILL_MAX_RESOURCE_BYTES` | `262144` | 单个 Skill 资源最大字节数 |
| `SKILL_MAX_RESOURCE_TOTAL_BYTES` | `1048576` | 单个 Skill 全部资源合计最大字节数 |

### 日志追踪与状态快照（tracing / checkpoint）

- **tracing**：日志行带 `[trace=xxxx]`，CLI 每轮对话一个 trace id，
  便于把散落的日志串成一条链路；
- **checkpoint**：每轮结束把运行状态（`turn / stop_reason / state`）写入
  `<SESSION_ID>.checkpoint.json`。它只存状态元数据，不含消息历史
  （历史在 `<SESSION_ID>.jsonl`）；
- **重试**：MCP 连接失败按 `MCP_CONNECT_RETRIES` 做指数退避；错误类别、插件
  声明的错误码与重试判定见上文“错误分类与重试（边界翻译）”。

### 事件总线（UNBOX）

进程内发布/订阅总线（`core/events.py`），两类事件语义严格区分：

| 类型 | API | 规则 |
|---|---|---|
| 观察类 | `bus.publish(event)` | 只读广播、异常隔离、支持 `subscribe("*")` |
| 决策类 | `bus.decide(event)` | 汇总 `allow/ask/deny`，按 `deny > ask > allow` 折叠，异常按 deny |

阶段一（当前）：

- loop 发布 `turn.start / model.request / model.response / model.error /
  tool.start / tool.after / tool.denied / turn.end`；
- `HookGateway.attach(bus)` 订阅观察事件并扇出给现有 hook 插件（**旧插件零迁移**），
  决策类 `tool.before` 仍由 HookGateway 直接承担；
- `MemoryGateway` 发布 `memory.write / memory.update / memory.delete`；
- CLI 发布 `session.start / session.end`，并在每轮输入前用
  `user_prompt.submit` 决策事件（返回 `deny` 即拒绝本轮）；
- 设置 `EVENT_LOG=<path>` 时自动订阅 `*`，把事件写成 JSONL 时间线。

第三方模块接入只需要一次订阅，不用改内核：

```python
from core.events import Event, EventBus

bus = EventBus()
bus.subscribe("tool.after", lambda event: print(event.name, event.payload["ok"]))
```

**listener 插件（`type: "listener"`）**：把“订阅”本身也做成插件——

```json
{
  "name": "timeline",
  "type": "listener",
  "events": ["*"],
  "entry": { "module": "listener.py", "factory": "create_listener" }
}
```

工厂返回 `core.events.Subscription(event, handler, priority)` 列表，loader 在
启动时把它们注册进总线（订阅了清单未声明的事件、或订阅决策类事件，启动即报错），
并返回退订令牌（为将来的热卸载预留）。**决策类事件不允许 listener 订阅**——
需要影响流程请写 hook 插件。完整示例见 `plugins/listeners/timeline/`。

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
  外部 `tool` runtime 同样可以执行任意命令，钩子插件是任意 Python 代码；
  只应安装可信来源的插件；
- **安装前受控检查**：zip 路径穿越、符号链接、包大小与入口逃逸会被拒绝；
  这些检查降低误装风险，但不把插件变成沙箱内代码；
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

## 插件平台协议（四大要求）

当前插件基座按 **Manifest / Capability Contract / Lifecycle / Versioned Protocol**
四项要求实现，详细决策见 `docs/adr/0013-plugin-platform-protocol.md`，清单结构见
`docs/plugin-manifest.schema.json`。

### Manifest

`plugin.json` 是唯一声明入口。单能力插件声明 `type + entry`；多能力包声明
`apiVersion + contributes[]`。发现阶段只读取和校验 JSON，不导入插件代码。

### Capability Contract

每个受信任的 `KindHandler` 声明支持的协议版本和产物接口。清单中的
`type + protocolVersion` 形成规范契约，例如 `hook.v1`、`tool.v1`；
如果显式写了 `contract`，必须与宿主计算结果一致。工厂返回值仍由对应接口验证，
例如 hook 必须返回 `LifecycleHooks`，tool 必须返回 `Tool`。

### Lifecycle

装配流程为：

```text
discover -> validate -> negotiate -> load -> instantiate
-> register -> setup(context) -> start -> ready -> stop
```

`setup(context)`、`start()`、`stop()` 是可选生命周期方法。Runtime 只激活当前
选中的能力；启动失败会逆序回滚已进入生命周期的插件，包括 `setup()` 失败的
当前插件（因此 `stop()` 必须可重复调用），正常关闭也会逆序释放资源。

### Versioned Protocol

清单可显式声明：

```json
{
  "apiVersion": "1",
  "protocolVersion": 1,
  "contract": "tool.v1"
}
```

`apiVersion` 是 manifest schema 版本，`protocolVersion` 是插件执行协议版本，
`version` 是插件自身版本。当前宿主只接受字段与 `KindHandler` 一致的协议版本，
不兼容插件会在导入代码前失败。

### 配置目录

配置统一放到 `config/`：

```text
config/
├── config.json
├── model.json
├── session.json
├── memory.json
├── embedding.json
├── context.json
├── mcp.json
└── plugins/
    ├── hook/permission.json
    └── <kind>/<name>.json
```

优先级为：环境变量 > 分域/单插件配置 > `config/config.json` > 内置默认。
运行时把 `config/plugins/<kind>/<name>.json` 通过 `PluginContext.config`
以只读形式交给插件。

## Roadmap

- 网页前端：拖入目录/zip 后调用同一个 `PluginManager`，展示校验结果与启停状态
- 插件更新与热重载：版本替换、运行时卸载与错误回滚
- 远程 HTTP MCP 插件（`transport: "http"` + URL + 服务器级信任）
- 钩子事件扩展（用户输入提交前、会话开始/结束等，对齐 Codex/Claude Code 拦截点）
- 长期记忆插件（`type: "memory"`：跨会话语义笔记 + recall 注入）
- MCP 网关进程化：把 `McpGateway` 换成独立代理进程/远程网关客户端（同一窄接口）
