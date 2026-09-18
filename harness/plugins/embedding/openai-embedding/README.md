# openai-embedding —— 嵌入提供方插件（真实语义）

`type: "embedding"`：调用 OpenAI Embeddings 接口批量生成向量，供
`plugins/memory/vector` 做语义检索。

## 配置

- `EMBEDDING_PROVIDER=openai-embedding`；
- `OPENAI_API_KEY`（必填，实例化时才校验）；
- `OPENAI_EMBEDDING_MODEL`（默认 `text-embedding-3-small`）、
  `OPENAI_BASE_URL` / `OPENAI_TIMEOUT` / `OPENAI_MAX_RETRIES`。
