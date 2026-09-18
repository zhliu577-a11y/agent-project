# debug —— 嵌入提供方插件（离线）

`type: "embedding"`：把字符散列到 32 维并 L2 归一化。**确定性、无网络、
零依赖**，用于离线跑通向量链路与测试；它只反映字符重叠，不是真正的语义向量。

```text
EMBEDDING_PROVIDER=debug   # 默认
```

换真实语义时改用 `openai-embedding`；接口同为 `core.embedding.EmbeddingProvider`。
