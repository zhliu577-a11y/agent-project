# Harness HTTP API

FastAPI 入口是 `api.main:app`，开发环境运行：

```powershell
python -m api
```

默认监听 `127.0.0.1:8000`。OpenAPI 文件由 FastAPI 自动生成：

```text
http://127.0.0.1:8000/openapi.json
http://127.0.0.1:8000/docs
```

前端应优先根据 `/openapi.json` 生成 TypeScript 类型和请求客户端，不手写重复的数据结构。

## API 分组

### 健康与 Runtime

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/health` | 进程与 Runtime 是否可用 |
| `GET` | `/api/v1/runtime` | Runtime 状态与插件、工具、MCP、模型、Skill 快照 |
| `POST` | `/api/v1/runtime/start` | 启动 Runtime |
| `POST` | `/api/v1/runtime/restart` | 关闭后重新装配 Runtime |

### 插件包

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/plugins` | 已安装插件及期望/实际状态 |
| `POST` | `/api/v1/plugins/inspect` | 上传 zip 并静态检查贡献与依赖，不安装 |
| `POST` | `/api/v1/plugins/install` | 安装 zip，安装后默认禁用 |
| `POST` | `/api/v1/plugins/{name}/enable` | 启用插件 |
| `POST` | `/api/v1/plugins/{name}/disable` | 禁用插件 |
| `DELETE` | `/api/v1/plugins/{name}` | 卸载插件 |

上传字段名为 `package`，只接受 `.zip`，单包上限与 `PluginManager` 一致。
安装、启用、禁用和卸载修改的是 Host registry；Runtime 正在运行时，客户端应检查
响应中的 `restartRequired`，随后调用 `/api/v1/runtime/restart`。

### 能力查询

| Method | Path |
| --- | --- |
| `GET` | `/api/v1/tools` |
| `GET` | `/api/v1/mcp` |
| `GET` | `/api/v1/mcp/preload` |
| `PUT` | `/api/v1/mcp/preload` |
| `GET` | `/api/v1/models` |
| `GET` | `/api/v1/models/settings` |
| `PUT` | `/api/v1/models/{name}/settings` |
| `POST` | `/api/v1/models/{name}/activate` |
| `GET` | `/api/v1/skills` |

`GET /api/v1/mcp/preload` 返回启动时加载的 MCP 插件、当前可用插件及每个 MCP 的运行状态。
`PUT /api/v1/mcp/preload` 接收 `preload` 和 `restart`：

```json
{
  "preload": ["filesystem", "math"],
  "restart": true
}
```

名称必须是当前 Runtime 已装配的 MCP 插件。页面选择写入本机 `data/mcp-config.json`，
不会修改仓库中的 `config/mcp.json`；`restart: true` 时保存后立即重新装配 Runtime。
保存或重启失败会恢复之前的本机覆盖配置。该文件已加入 `.gitignore`。

`GET /api/v1/models/settings` 返回可编辑字段，但不会回显 `apiKey`，只返回
`hasApiKey`。`PUT /api/v1/models/{name}/settings` 接受 `apiKey`、`baseUrl`、
`model`、`timeout`、`maxRetries` 和 `disableResponseStorage`。空 `apiKey`
表示保留现有值；`clearApiKey: true` 可清除本地覆盖层中的 key。保存后 Runtime
会重新装配，并按 `activate` 决定是否切换到该模型。

页面填写的模型配置保存在本机 `data/model-configs.json`，不会修改仓库中的
示例配置，也不会通过 API 回传密钥。该文件已加入 `.gitignore`。

`POST /api/v1/models/{name}/activate` 在运行时热切换显式模型。Runtime 会先
初始化目标 provider，成功后才更新 active model；切换失败时保留原模型。对话
进行中调用该接口返回 `409`。该选择只作用于当前 Runtime，重启后恢复
`config/model.json` 中的默认 `model`。

### 对话

`POST /api/v1/chat` 返回完整结果：

```json
{
  "message": "帮我检查这个项目",
  "maxTurns": 20
}
```

`POST /api/v1/chat/stream` 返回 `text/event-stream`。当前事件类型：

| Event | Data |
| --- | --- |
| `token` | `{ "delta": "..." }` |
| `runtime` | 结构化 observation event |
| `approval` | 待处理的 Skill、Tool 或 Prompt 审批 |
| `done` | 本轮完整 `TurnResult` |
| `error` | 对话失败的类型和消息 |

浏览器必须逐条读取 SSE，不能假设一次网络响应只含一个事件。连接断开时，客户端
应允许重新发起新一轮；宿主侧会把已完成轮次提交到 Session。

### 工作区活动

`GET /api/v1/events` 返回经过脱敏的只读事件日志，用于展示“谁在什么时候做了什么”。

| 参数 | 说明 |
| --- | --- |
| `after` | 返回序号大于该值的增量记录 |
| `limit` | 最多返回的记录数，默认 `200`，最大 `1000` |
| `name` | 按事件名模糊筛选 |
| `publisher` | 按发布者身份筛选 |
| `sessionId` | 精确筛选 Session |
| `runId` | 精确筛选 Run |
| `traceId` | 精确筛选 Trace |

每条记录包含 `seq`、`eventId`、`timestamp`、`name`、`publisher`、`status`、
`target`、`summary` 和脱敏后的 `payload`。字段名包含 `key`、`token`、
`password`、`secret` 或 `auth` 时，值统一替换为 `[REDACTED]`。

### 审批

需要人工确认时，Runtime 暂停对应调用并返回：

1. SSE `approval` 事件；
2. `GET /api/v1/approvals` 中也可查询当前待处理项；
3. 客户端调用 `POST /api/v1/approvals/{approval_id}` 同意或拒绝。

```json
{
  "approved": true,
  "reason": "本次允许"
}
```

审批超时由 `APPROVAL_TIMEOUT` 控制，默认 300 秒。超时按拒绝处理。

## 边界

- HTTP 层不直接执行插件代码，也不绕过 Gateway。
- 对话轮次由 `ConversationService` 串行化，单个 Runtime 同时只处理一轮。
- 插件上传先写入 staging，再由 `PluginManager` 解压、校验、摘要和原子安装。
- 插件启停不热替换当前 Runtime；需要重启才能更新对象图。
