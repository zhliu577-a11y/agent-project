# ADR 0019: Memory Store and Recall Strategy Separation

- Status: accepted
- Date: 2026-09-16

## Context

The first memory design coupled recall to storage. `MemoryGateway` either called
`MemoryStore.search_notes()` directly or branched to a `MemoryIndex`. That made
vector recall, lexical recall, recency recall, and future hybrid recall compete
inside host code instead of being selected as capabilities.

Storage and retrieval also have different lifecycles. A store owns durable
records, while a retriever may own derived state, query an external service, or
combine several candidate sources.

## Decision

Recall is represented by the `memory-retriever` kind and the
`MemoryRetriever` protocol:

- `retrieve(query)` returns candidate records.
- `rebuild(records)` rebuilds optional derived state from durable records.
- `add(record)` and `remove(record_id)` keep optional derived state in sync.

`MemoryGateway` is the only writer and always resolves exactly one retriever:

1. an explicitly injected `retriever`;
2. a compatibility adapter around a legacy `memory-index`;
3. `StoreMemoryRetriever`, which delegates to the selected store.

`MemoryStore` and `MemoryRetriever` remain separate protocols, but one plugin
package may contribute both. The built-in `memory-vector` package does this:
its `vector` contribution owns vector persistence and its `vector-native`
contribution recalls through that store. Host configuration can still select
the store and retriever independently.

Package contribution aliases, such as `vector`, resolve to canonical
`<package>--<contribution>` service names. Runtime registration exposes both
forms so a retriever can request `memory/vector` without knowing the package
name.

## Compatibility

`memory-index` and `MEMORY_INDEX` are retained for existing installations. If
only the legacy index is configured, Runtime injects
`LegacyMemoryIndexRetriever`. Configuring both `MEMORY_INDEX` and
`MEMORY_RETRIEVER` is rejected because recall would have two owners.

The default is `MEMORY_RETRIEVER=store-native`. The legacy `lexical` and
`recent` index implementations are also available as standalone retrievers.

## Consequences

The host no longer guesses how recall works. Vector, lexical, recency, remote,
and hybrid retrieval can evolve as plugins while stores keep responsibility for
persistence. Existing index plugins continue to work, and new packages can
version a store and its matching retriever together.
