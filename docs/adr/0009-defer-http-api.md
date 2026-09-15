# ADR 0009：暂时移除 HTTP API

- 状态：已采纳
- 日期：2026-09-15

## 背景

当前 `api/main.py` 提供了 chat、session、memory 和 plugin 概览接口，但前端
形态尚未确定。后续方向可能是 CLI 或网页，网页还需要支持拖入目录/zip 安装
插件。继续维护一套临时 HTTP 接口会同时锁定请求模型和运行时装配方式。

## 决策

删除当前 `api/` 实现以及 FastAPI、uvicorn 依赖，先提供 CLI 作为第一个客户端。

CLI 和未来网页前端都调用同一个 `PluginManager`：

```text
CLI      ┐
Web UI   ├── PluginManager ── PluginRegistry + plugin-store
Other    ┘
```

未来重写 API 时，不复制生命周期逻辑，只做 HTTP 输入输出适配。

## 后果

- 当前不能通过 HTTP 使用 Harness，只能运行 CLI。
- API 请求与鉴权模型可以在前端需求明确后重新设计。
- 插件生命周期先形成稳定领域接口，避免网页直接操作文件系统。
- 历史 ADR 保留，文档明确记录当前 API 已暂时移除。
