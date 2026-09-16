# ADR 0018: Memory Lifecycle and Automatic Extraction

- Status: accepted
- Date: 2026-09-16

## Context

The memory store, derived index, and policy plugins existed, but durable memory
still lacked a controlled automatic extraction path and complete lifecycle
semantics. Identity fields were also too narrow for future multi-agent and
multi-tenant execution.

Allowing an extractor to write storage directly would bypass policy, indexing,
events, and ACL checks. Keeping extraction in the agent loop would also couple
the fixed loop to one memory strategy.

## Decision

Memory is split into four independently selected capabilities:

- `memory` owns durable records and is the only source of truth.
- `memory-retriever` owns recall strategy; `memory-index` remains a legacy
  candidate-view compatibility protocol.
- `memory-policy` decides write admission, reconciliation, ranking, and
  expiration visibility.
- `memory-extractor` converts committed messages into candidates only.

`MemoryExtractionGateway` runs after `SessionGateway.commit_turn()` succeeds.
Extraction failures are non-fatal. Every candidate is written through
`MemoryGateway.remember()`, so identity checks, ACLs, deduplication,
supersede decisions, index updates, and events remain centralized.

`MemoryGateway` also owns:

- exact normalized duplicate detection through policy reconciliation;
- explicit supersede decisions that mark the previous record without hard
  deletion;
- expiration maintenance that marks records as `expired` and removes them
  from derived indexes;
- `last_accessed_at` and `metadata.accessCount` updates after recall;
- `scope`, `owner_id`, `agent_id`, and `tenant_id` propagation and filtering.

## Consequences

Extractors remain replaceable without becoming storage owners. A failed
extractor or candidate write cannot fail the user turn. Different stores and
indexes preserve the same lifecycle contract, while policies can choose stricter
or looser reconciliation rules.

Automatic extraction currently runs once after a successful session commit.
Future extractors may use models or additional context, but they must keep the
same candidate-only contract.
