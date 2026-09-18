# vector —— 长期记忆插件（语义检索）

这是一个功能包，同时贡献两个能力：

- `memory/vector`：在 SQLite 中额外保存每条笔记的 embedding；
- `memory-retriever/vector-native`：把 query 转向量、按余弦相似度取 Top-K；
  相似度低于阈值时回退 Store 的字面/标签搜索。

因此 Store 和匹配的召回实现可以一起发布和版本化，同时仍由宿主的
`MEMORY_STORE`、`MEMORY_RETRIEVER` 独立选择。

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `MEMORY_STORE` | - | 设为 `vector` 启用本后端 |
| `MEMORY_RETRIEVER` | `store-native` | 设为 `vector-native` 使用本包的原生向量召回 |
| `MEMORY_VECTOR_DB_PATH` | `.memory/vector.db` | 数据文件 |
| `EMBEDDING_PROVIDER` | `debug` | 嵌入来源（plugins/embedding/*） |
| `VECTOR_SIMILARITY_THRESHOLD` | `0.2` | 低于该值回退字面搜索 |
| `VECTOR_TOP_K` | `3` | 最多返回条数 |

真实语义检索需配 `EMBEDDING_PROVIDER=openai-embedding` + `OPENAI_API_KEY`。
