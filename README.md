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
