# agent-project

项目采用前后端分目录结构：

```text
agent-project/
├── harness/    # Python Agent Harness、插件、配置和测试
├── frontend/   # 前端应用工作区
└── .github/    # 仓库级 CI
```

后端命令从 `harness/` 目录运行，例如：

```powershell
cd harness
..\.venv\Scripts\python.exe -m pytest -q
```

启动后端 HTTP API：

```powershell
cd harness
..\.venv\Scripts\python.exe -m api
```

默认监听 `http://127.0.0.1:8000`，接口文档位于
`http://127.0.0.1:8000/docs`。

请在普通 PowerShell 终端中启动后端。MCP 使用 stdio 子进程通信；如果把后端
放进受限沙箱、受限后台任务或禁止创建子进程管道的环境中，Windows 可能在连接
MCP 时返回 `[WinError 5] 拒绝访问`。

内置 `filesystem` MCP 不依赖 `npx`、npm 缓存或外网。它只访问项目内沙箱：

```text
harness/data/workspace
```

启动时可以通过 `mcp.preload` 或 `MCP_PRELOAD` 预加载高频 MCP；其他 MCP 保持
`idle`，由 Agent 在需要时调用 `use_plugin` 按需连接，而不是启动时全部拉起。

启动前端控制台：

```powershell
cd frontend
npm install
npm run dev
```

默认访问 `http://127.0.0.1:5173`。前端开发服务器会把 `/api` 代理到
`http://127.0.0.1:8000`。

前端校验命令：

```powershell
cd frontend
npm run typecheck
npm run build
npm run verify:ui
```

`verify:ui` 需要后端和 Vite 开发服务器保持运行。
