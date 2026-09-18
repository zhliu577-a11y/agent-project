# Harness Console

Harness 的本地控制台，负责 Runtime 状态、插件包管理、能力目录、Agent 对话和审批。

## 技术栈

- React 19
- TypeScript
- Vite
- lucide-react
- 原生 Fetch 与 SSE，不额外引入状态管理库

## 本地开发

后端默认监听 `http://127.0.0.1:8000`：

```powershell
cd harness
..\.venv\Scripts\python.exe -m api
```

另开终端启动前端：

```powershell
cd frontend
npm install
npm run dev
```

访问 `http://127.0.0.1:5173`。Vite 会把 `/api` 代理到后端。

## 校验

```powershell
npm run typecheck
npm run build
npm run verify:ui
```

`verify:ui` 使用本机 Edge 或 Chrome，检查桌面和移动端的导航、核心页面、
浏览器错误以及横向溢出。运行前需要保持后端和 Vite 开发服务器在线。

## 页面

- `overview`：Runtime 生命周期、模型、MCP、插件和审批概况
- `plugins`：上传、检查、安装、启停和移除插件包，并按贡献类型分类
- `capabilities`：Tool、MCP、Model 和 Skill 运行时目录，可配置 MCP 启动时加载
- `chat`：SSE 流式对话、运行事件和审批处理
- `activity`：按时间查看发布者、目标、状态和 Trace 的脱敏活动记录

API 契约见 [harness/docs/api.md](../harness/docs/api.md)。
