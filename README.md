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
