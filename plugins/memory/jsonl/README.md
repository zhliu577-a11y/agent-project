# jsonl —— 长期记忆插件（可读示例）

`type: "memory"`：全部笔记写在 `.memory/memory.jsonl`，一条笔记一行 JSON，
可以直接打开审阅/手工编辑。适合调试与教学，不建议在高频场景使用。

## 配置

```text
config.json / 环境变量: MEMORY_STORE=jsonl
数据目录:              MEMORY_DATA_DIR，默认 .memory/
```

实现与 sqlite 后端相同的 `MemoryStore` 契约，可随时切换。
