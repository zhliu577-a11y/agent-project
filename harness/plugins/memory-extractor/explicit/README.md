# explicit memory extractor

`memory-extractor.v1` plugin that turns explicit user requests such as
`记住...`, `remember that ...`, and `I prefer ...` into durable memory
candidates.

The extractor never writes storage directly. Runtime passes candidates through
`MemoryGateway`, where identity, policy, deduplication, and lifecycle rules are
applied.
