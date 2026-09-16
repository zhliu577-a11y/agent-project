# ADR 0017: Runtime Service Graph and Memory Boundaries

- Status: accepted
- Date: 2026-09-16
- Depends on: ADR 0002, ADR 0013, ADR 0016
- Refined by: ADR 0019 (memory recall strategy separation)

## Context

Plugin kinds already had contracts, but the runtime still constructed selected
services in handwritten order. Dependencies were hidden inside plugin code. The
most visible example was vector memory scanning `plugins/` and reading
`EMBEDDING_PROVIDER` itself instead of receiving the embedding service selected
by the runtime.

That design makes replacement, initialization order, version checks, and
rollback host responsibilities without a host-side dependency model.

## Decision

1. `plugin.json` may declare `requires` entries with `kind`, optional `name`,
   optional `contract`, `required`, and `inject`.
2. `RuntimeServices` is process-local and shared by all plugin contexts created
   by one `HarnessRuntime`.
3. Selected service plugins are resolved as a dependency-first graph. Cycles,
   missing required services, and contract mismatches fail startup.
4. Plugin factories may receive resolved dependencies as keyword arguments.
   The manifest's `inject` value defaults to the kind name with hyphens replaced
   by underscores.
5. Gateway construction remains host-owned. The graph constructs capability
   instances; `SessionGateway`, `MemoryGateway`, and `ContextGateway` define
   transaction and orchestration boundaries.
6. Vector memory receives `EmbeddingProvider` through injection and no longer
   discovers embedding plugins itself.

## Memory-specific boundaries

```text
SessionGateway
  -> SessionStore plugin
  -> CompactionPolicy plugin

MemoryGateway
  -> MemoryStore plugin
  -> MemoryRetriever plugin (or legacy MemoryIndex adapter)
  -> optional MemoryPolicy plugin
  -> optional MemoryExtractor plugin
  -> optional EmbeddingProvider used by the selected store

ContextGateway
  -> ContextPolicy plugin
  -> optional MemoryRecallPort
```

`MemoryRetriever` owns candidate recall; it may query `MemoryStore` directly or
maintain derived representation that can be rebuilt from it. `MemoryPolicy`
owns write acceptance and recall ranking. Neither owns the source record.
`MemoryIndex` remains available as a legacy adapter.

The three memory-facing protocols remain separate:

- `ContextPolicy` is read-only and selects the messages for one model call.
- `CompactionPolicy` proposes a replacement for persisted session history; only
  `SessionGateway` commits it.
- `MemoryStore`, `MemoryRetriever`, and `MemoryPolicy` own durable facts,
  recall candidates, and admission or ranking rules across sessions.

`ContextGateway` depends only on `MemoryRecallPort`, which exposes
`context_records()` but no write operation. A recall failure is isolated:
context assembly logs the error and continues with an empty memory contribution
instead of bypassing or failing the selected `ContextPolicy`.

The default `memory-tail-window` context plugin consumes
`state["memory.records"]`, reserves a bounded part of the model budget for
those records, and appends them to the system context as reference data. Pure
`tail-window` and `summary-window` remain available when automatic memory
injection is not desired.

Built-in retriever plugins are `store-native`, `lexical`, and `recent`.
Built-in policy plugins are `default` (importance, confidence, recency) and
`strict` (quality admission or confidence-first ranking).

## Multi-agent isolation

`MemoryRecord` carries `scope`, `owner_id`, `agent_id`, and `tenant_id`.
`MemoryGateway` can restrict each dimension and applies the checks to writes,
recall, delete, and update. This is the host-controlled ACL boundary; agent
prompts do not decide visibility.

## Consequences

- Changing an embedding implementation is a config and assembly concern, not a
  vector-memory code change.
- A plugin can declare hard or optional dependencies without importing another
  implementation.
- The host can report missing and incompatible dependencies before running the
  agent loop.
- Context and compaction plugins cannot mutate durable memory through the
  context-assembly port.
- New service kinds still require a trusted `register_kind()` entry; manifests
  cannot register executable runtime stages.
