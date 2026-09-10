# sqlite —— 长期记忆插件（生产默认）

`type: "memory"`：语义笔记存在 SQLite 中，事务写入、WAL 模式、
参数化查询、`created_at` 索引。

## 配置

- 选择：`config.json` 的 `"memory": { "store": "sqlite" }` 或 `MEMORY_STORE=sqlite`；
- 数据文件：`MEMORY_DB_PATH`，默认 `.memory/memory.db`（已 gitignore）。

模型侧工具：`remember` / `recall` / `update_note` / `forget`
（由 `core.memory.MemoryStore` 契约 + 网关提供，工具名不带插件命名空间）。
