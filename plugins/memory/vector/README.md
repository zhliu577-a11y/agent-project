# vector —— 长期记忆插件（语义检索）

`type: "memory"`：在 SQLite 中额外保存每条笔记的 embedding，
`recall` 时把 query 转向量、按余弦相似度取 Top-K；相似度低于阈值自动回退
子串/标签搜索。

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `MEMORY_STORE` | - | 设为 `vector` 启用本后端 |
| `MEMORY_VECTOR_DB_PATH` | `.memory/vector.db` | 数据文件 |
| `EMBEDDING_PROVIDER` | `debug` | 嵌入来源（plugins/embedding/*） |
| `VECTOR_SIMILARITY_THRESHOLD` | `0.2` | 低于该值回退字面搜索 |
| `VECTOR_TOP_K` | `3` | 最多返回条数 |

真实语义检索需配 `EMBEDDING_PROVIDER=openai-embedding` + `OPENAI_API_KEY`。
