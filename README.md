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
  拖入即可被 Agent 发现；目前支持 `mcp`、`hook`、`tool`、`model`、`skill`、
  `session`、`memory`、`embedding`、`listener` 九类
- **功能包**：一个包可用 `contributes[]` 同时提供 skill、hook、tool 等能力，
  发现阶段统一展开为 contribution 后按受控 kind 注册表装配
- **MCP 网关**：Agent 只面向网关这一个通道；网关统一维护各 MCP 插件的连接、
  会话、命名与清理，工具名带命名空间（`<插件名>__<工具名>`）
- **钩子网关**：生命周期钩子全部插件化（`turn_start / llm_response /
  tool_before / tool_after / turn_end`），内核只面向 `HookGateway`
- **本地工具插件**：高频轻量能力以进程内 Python 函数提供（`plugins/tools/*`），
  启动即注册，无子进程、无挂载步骤
- **模型插件**：LLM 适配器来自 `plugins/model/*`（当前 deepseek 为默认），
  `AGENT_MODEL` 环境变量即可切换模型提供方
- **技能插件**：纯内容插件（`plugins/skills/*`）按需注入操作说明——启动只放
  目录条目，模型需要时用 `use_skill` 读取完整正文（渐进披露）
- **会话插件**：短期记忆插件化（`plugins/session/*`）——同一会话多轮历史
  自动延续，退出后按 `SESSION_ID` 恢复
- **长期记忆插件**：跨会话语义笔记（`plugins/memory/*`），模型通过
  `remember / recall / forget` 主动读写
- **上下文模块**：token 估算 + 每轮自动裁剪（`CONTEXT_MAX_TOKENS`），
  历史只增不减的问题有解
- **插件包生命周期**：外部目录或 zip 包经过静态检查、staging、摘要计算后原子安装；
  安装后默认禁用，启停状态由 `data/plugin-registry.json` 管理
- **CLI**：`python -m cli plugin ...` 提供 validate / install / enable / disable /
  remove / list；后续网页前端复用同一套 `PluginManager`
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
│  permission / audit  │   │  plugins/tools/*（text…，启动即注册）        │
└──────────────────────┘   └────────────────────────────────────────────┘
```

运行链路：启动时扫描 `plugins/` 与 registry 中已启用的外部包 →
钩子插件实例化进 `HookGateway`、本地工具
直接注册进 `ToolRegistry`、MCP 插件只读清单 → 本地工具第一轮即可调用；MCP
工具由模型决定调 `use_plugin` → 网关连接对应 MCP 插件并注册命名空间工具
（都先过权限钩子闸门）→ 结果回填 → 模型给出最终回答。

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
├── tools/                      # 本地工具插件（进程内，启动即注册）
│   ├── text/                   #   文本工具示例（slugify / count_words）
│   └── json/                   #   JSON 处理（format / get）
│       ├── plugin.json
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
  "name": "time",
  "type": "mcp",
  "version": "1.0.0",
  "description": "查询指定时区的当前时间",
  "enabled": true,
  "entry": {}
}
```

`name` 只允许 `A-Z a-z 0-9 _ -`（会进入工具命名空间）；`type` 当前支持
`mcp` / `hook` / `tool` / `model` / `skill` / `session` / `memory` / `embedding` / `listener`；
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
  "contributes": [
    {
      "id": "lint",
      "kind": "skill",
      "entry": { "content": "skills/lint/SKILL.md" }
    },
    {
      "id": "format-hook",
      "kind": "hook",
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
| 高频轻量、纯函数、内部逻辑 | 是（零成本） | 偏重 |
| 需要现成生态（npx / node / docker） | 否 | 是 |
| 需要独立进程/沙箱隔离 | 否 | 是 |
| 将来要让别的 MCP 客户端复用 | 否 | 是 |

两种工具在模型侧无差别：都是 `<插件名>__<工具名>`，都过同一套权限/审计钩子；
区别只在“能力怎么托管”。

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
由 Harness 根据 `AGENT_MODEL`（默认 `deepseek`）选出激活插件后惰性创建。
仓库自带两个示例：`deepseek`（默认）与 `openai`。换模型 = 复制
`plugins/model/openai/` 目录改成自己的适配器（或直接设
`AGENT_MODEL=openai` 并用 `OPENAI_*` 配置），互不影响。

模型插件只负责**传输**（鉴权 / base_url / 请求体 / 取流）；把流式分片翻译成
`ModelResponse` 的解析逻辑由内核 [core/parser.py] 提供（`ResponseParser` +
`ToolCallAccumulator` + `OpenAICompatibleParser`），deepseek/openai 直接复用，
新接 OpenAI 兼容 provider 不必再抄一遍解析代码。

### 技能插件（`type: "skill"`）

```json
{
  "name": "code-review",
  "type": "skill",
  "entry": { "content": "SKILL.md", "preload": false }
}
```

技能是**纯内容插件**：不执行代码、不注册工具，只是一份 Markdown 操作说明。
渐进披露规则：

- 启动时系统提示词里只有“目录条目”（技能名 + 一句话描述）；
- 模型需要某技能时调用 `use_skill(name)`，网关才读取该插件正文并回填
  （惰性读取 + 缓存），`use_skill` 同样过权限/审计钩子；
- 全局规则类技能可显式 `"preload": true` 在启动时注入系统提示词，
  但默认关闭，避免上下文膨胀。

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

### 上下文模块

- 上下文预算：`CONTEXT_MAX_TOKENS`（默认 20000），每轮请求前按
  “丢最旧、保最近”裁剪历史，裁剪数量会打日志。

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

### `config.json`（中心配置，随仓库提交）

只承载“选择型配置”，优先级 **环境变量 > config.json > 内置默认**；
密钥与数据目录仍只放 `.env`。

```json
{
  "model": "deepseek",
  "session": { "store": "jsonl", "id": "default" },
  "memory": { "store": "sqlite" },
  "embedding": { "provider": "debug" },
  "context": { "maxTokens": 20000 }
}
```

对应字段写错启动即报错（`core/config.py` 校验）；想临时换后端，用环境变量
覆盖即可（例如 `SESSION_STORE=inmemory`）。

### `.env`（密钥与环境，已 gitignore，绝不提交）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 无 | DeepSeek API Key（必填） |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容接口地址 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 模型名 |
| `DEEPSEEK_TIMEOUT` | `60` | 单次模型请求超时（秒） |
| `DEEPSEEK_MAX_RETRIES` | `3` | 模型请求重试次数（仅安全场景） |
| `AGENT_MODEL` | `deepseek` | 激活的模型插件名（plugins/model/* 里选） |
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
| `CONTEXT_MAX_TOKENS` | `20000` | 单轮模型上下文预算（超出丢最旧历史） |
| `MCP_CONNECT_TIMEOUT` | `20` | MCP 插件连接超时（秒） |
| `MCP_CALL_TIMEOUT` | `30` | 单次 MCP 工具调用超时（秒） |
| `MCP_CONNECT_RETRIES` | `0` | MCP 连接失败额外重试次数（指数退避，默认不重试） |

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
  钩子插件是任意 Python 代码；只应安装可信来源的插件；
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

## Roadmap

- 网页前端：拖入目录/zip 后调用同一个 `PluginManager`，展示校验结果与启停状态
- 插件更新与热重载：版本替换、运行时卸载与错误回滚
- 远程 HTTP MCP 插件（`transport: "http"` + URL + 服务器级信任）
- 钩子事件扩展（用户输入提交前、会话开始/结束等，对齐 Codex/Claude Code 拦截点）
- 长期记忆插件（`type: "memory"`：跨会话语义笔记 + recall 注入）
- 上下文预算 hook（对话过长时自动截断/摘要）
- MCP 网关进程化：把 `McpGateway` 换成独立代理进程/远程网关客户端（同一窄接口）
