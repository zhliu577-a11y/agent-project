# agent-project

一个"固定 agent loop + 插件化扩展"的精简 agent：**内核只做编排，能力全部可插拔、可配置**。

## 特性

- 固定异步 agent loop（调模型 → 执行工具 → 回填 → 判断结束），支持流式输出与多个工具的并行执行
- 模型适配器接口，默认接 DeepSeek（OpenAI 兼容协议，可换 OpenAI / 通义 / 本地 vLLM 等）
- MCP 工具桥接：多服务器、连接/调用超时保护、失败自动清理
- 插件目录 + 按需加载：模型通过 `use_server` 自行决定加载哪台服务器
- 权限策略：`allow / ask / deny`，支持通配符；被拒绝的工具**不会执行**
- 配置文件带 schema 校验：写错字段启动即报错，绝不静默出错
- 单元测试 + ruff 规范 + GitHub Actions CI

## 架构

```text
┌──────────────────────── 内核（固定，不随插件变化）────────────────────────┐
│  loop.py       固定循环：调模型 → 工具 → 回填                           │
│  core/         类型与接口：types / Tool / ToolRegistry / hooks / model  │
│                生命周期钩子（turn_start / llm_response / tool_before…） │
└──────────────┬──────────────────────────────────────────────────────────┘
               │ 实现接口
┌──────────────▼──────────────────────────────────────────────────────────┐
│ 扩展层（配置驱动）                                                      │
│  models/openai_compat.py    DeepSeek/OpenAI 兼容模型（流式 + 重试）      │
│  mcp_bridge.py              MCP 客户端桥接（stdio，多服务器）            │
│  plugin_loader.py           读 mcp_servers.json 目录 + use_server 加载   │
│  permission.py              读 permission.json 策略，tool_before 闸门    │
└──────────────────────────────────────────────────────────────────────────┘
```

运行链路：用户输入 → loop 调模型 → 模型决定调 `use_server` → 桥接连接 MCP 服务器并注册工具 → 模型调用工具（先过权限闸门）→ 结果回填 → 模型给出最终回答。

## 快速开始

要求：Python 3.13+，Windows / macOS / Linux。

```powershell
python -m venv .venv
.venv\Scripts\activate          # Windows；macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

复制 `.env.example` 为 `.env`，填入 `DEEPSEEK_API_KEY`（到 https://platform.deepseek.com 创建），然后：

```powershell
.venv\Scripts\python.exe main.py
```

## 配置说明

### `.env`（密钥与环境，已 gitignore，绝不提交）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 无 | DeepSeek API Key（必填） |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容接口地址 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 模型名 |
| `DEEPSEEK_TIMEOUT` | `60` | 单次模型请求超时（秒） |
| `DEEPSEEK_MAX_RETRIES` | `3` | 模型请求重试次数（仅安全场景） |
| `MCP_CONNECT_TIMEOUT` | `20` | MCP 服务器连接超时（秒） |
| `MCP_CALL_TIMEOUT` | `30` | 单次 MCP 工具调用超时（秒） |

### `mcp_servers.json`（插件目录，schema 校验）

```json
{
  "plugins": [
    {
      "name": "time",
      "description": "查询指定时区的当前时间",
      "enabled": true,
      "mcp": { "command": "python", "args": ["mcp_servers/time_server.py"] }
    },
    {
      "name": "filesystem",
      "description": "读写本地文件",
      "enabled": true,
      "mcp": {
        "command": "cmd.exe",
        "args": ["/c", "npx", "-y", "@modelcontextprotocol/server-filesystem", "E:/path/to/sandbox"]
      }
    }
  ]
}
```

字段规则：`name` 必填非空；`mcp.command` 必填非空；`mcp.args` 必须是字符串数组；`enabled: false` 的插件会被跳过（但结构仍会校验）。写错字段程序会报错退出，不会静默出错。

> 外部 MCP：任意可执行命令（python / node / npx / docker）都能作为 `command`，所以社区或官方 MCP 服务器可以直接加进目录按需加载。Windows 下 npm 包类服务器建议用 `cmd.exe` + `/c npx ...`。

### `permission.json`（工具权限策略）

```json
{
  "default": "allow",
  "rules": [
    { "tool": "shell", "mode": "deny" },
    { "tool": "move_file", "mode": "ask" },
    { "tool": "write_file", "mode": "ask" },
    { "tool": "delete_file", "mode": "deny" }
  ]
}
```

`mode` 只能是 `allow / ask / deny`；`tool` 支持通配符（如 `delete_*`）。校验不通过同样启动即报错。

## 权限与安全模型

- **按需加载**：启动时只读目录、不连接任何服务器；模型通过 `use_server` 触发连接，避免无谓的进程与上下文开销；
- **工具级权限闸门**：loop 在每次工具执行前调用 `tool_before`，任一钩子返回拒绝则工具不执行（结果"权限拒绝"回填给模型）；
- **策略默认放行**：危险工具需要你在 `permission.json` 显式配置 `ask/deny` 才受保护；
- **MCP 服务器信任**：本地 stdio 服务器应来自可信来源（可审查代码）；远程 HTTP 服务器（未来支持）还需要服务器级信任确认；
- **密钥管理**：API Key 只放 `.env`，已被 gitignore 排除，禁止提交；
- **配置即代码审核面**：所有扩展点（服务器、权限）都是可 diff 的 JSON，便于 review。

## 测试与代码规范

```powershell
.venv\Scripts\python.exe -m pytest -q    # 单元测试
ruff check .                             # 静态检查
ruff format .                            # 统一格式
```

GitHub Actions（`.github/workflows/ci.yml`）在每次 push / PR 时自动执行 lint + format 检查 + 全部测试。

## 设计原则

1. 贴近真实生产环境，不因教学简单而妥协设计。
2. 内核固定，能力插件化；插件通过类型化接口接入。
3. 重要决策记录为 ADR（架构决策记录），不靠口头约定。
4. 配置外置（环境变量/配置文件），密钥绝不进入版本库。
5. 可观测性优先：结构化日志、异常隔离、可回放。
6. 代码必须配套测试，测试通过才允许合并。
7. LLM 优先、流程从简：内核只提供最小行动能力，规划与工具组合交给模型本身。

## Roadmap

- 远程 HTTP MCP（`type: "http"` + URL + 服务器级信任）
- 会话持久化 / 上下文长度管理
- Skills 型插件（按需注入操作说明）
- 多服务器工具命名空间隔离（避免同名工具冲突）
- 本地工具插件（不依赖 MCP 的纯函数工具）
