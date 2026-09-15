# jsonl —— 会话存储插件（默认）

`type: "session"` 的文件后端：每个会话一个 `<SESSION_ID>.jsonl`（每条消息一行），
外加 `<SESSION_ID>.checkpoint.json` 保存轻量运行状态（turn/stop_reason/state）。

## 配置

- 选择：`config/session.json` 的 `{ "store": "jsonl", "id": "default" }`，
  或环境变量 `SESSION_STORE` / `SESSION_ID`（环境变量优先）；
- 数据目录：`SESSION_DATA_DIR`，默认项目下 `.sessions/`（已 gitignore）。

实现 `core.session.SessionStore`；当前为全量覆写，规模变大后可换其它后端。
