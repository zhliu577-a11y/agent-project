# ADR 0006: Observation event bus

- Status: superseded by ADR 0015
- Date: 2026-09-10
- Updated: 2026-09-16
- Depends on: ADR 0001 (plugin gateways), `core/hooks.py`, `core/tracing.py`
- Follow-up: ADR 0015 (EventGateway and pluggable event transport)

## Context

The core originally exposed only typed hook calls. Audit, tracing, UI updates,
timeline projections, and future agents also need to observe facts such as
turn start, model response, tool completion, memory writes, and session
lifecycle without coupling publishers to subscribers.

## Decision

Events are observation-only structured facts:

```python
Event(
    name="tool.after",
    payload={"ctx": ..., "tool_call": ..., "result": ..., "ok": True},
    trace_id="ab12cd34",
)
```

Event names use `<domain>.<action>`. Payloads may carry in-process objects;
serialization sinks apply their own safe conversion and size limits.

### Observation delivery

`publish(event)` means:

- the publisher does not know or wait for subscribers;
- no subscriber can change the publisher's return value;
- subscriber failure or timeout cannot break the agent loop;
- wildcard subscriptions are allowed;
- delivery may be queued and delayed.

### Control plane

The event bus does **not** carry allow/ask/deny decisions. Control calls must
return synchronously:

| Concern | Direct owner |
|---|---|
| User prompt admission | `HookGateway.user_prompt_submit()` |
| Tool permission | `HookGateway.tool_before()` |
| Model call | `ModelAdapter.complete()` |
| Tool call | `ToolRegistry.execute()` |
| Session/memory/context | Their gateway or policy object |

After a decision, the system may publish observations such as
`user_prompt.accepted`, `user_prompt.rejected`, or `tool.denied`.

## Event examples

| Event | Publisher |
|---|---|
| `turn.start` / `turn.end` | agent loop |
| `model.request` / `model.response` / `model.error` | agent loop |
| `tool.start` / `tool.after` / `tool.denied` | agent loop |
| `memory.write` / `memory.update` / `memory.delete` | memory gateway |
| `skill.loaded` / `skill.preloaded` / `skill.resource_loaded` / `skill.load_failed` / `skill.resource_failed` | skill runtime |
| `session.start` / `session.end` | CLI or future API |
| `user_prompt.accepted` / `user_prompt.rejected` | CLI or future API |

## Boundaries

- Observation subscribers never control the main execution path.
- The event bus is not a service locator or request/response bus.
- Event history, cursors, replay, and projections belong to a separate
  EventStore.
- Multi-agent task dispatch, replies, acknowledgements, and retries belong to
  a future MessageBus, not this observation bus.
