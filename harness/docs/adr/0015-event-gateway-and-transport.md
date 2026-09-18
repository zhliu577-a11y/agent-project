# ADR 0015: Host event gateway and pluggable event transport

- Status: accepted
- Date: 2026-09-16
- Depends on: ADR 0006 (Event Bus), ADR 0013 (Plugin Platform Protocol)

## Decision

The host owns the logical event system. Transport is replaceable through the
trusted `event-transport.v1` plugin kind.

```text
internal publishers / listeners / hooks
                |
                v
          EventGateway
                |
                v
          EventTransport
```

`EventGateway` is a host service, not a plugin. It owns:

- subscription tables and unsubscribe tokens;
- exact-name and wildcard matching;
- session/run/turn/agent/trace scope filtering;
- priority and registration ordering;
- trusted publisher/subscriber identity binding;
- subscriber timeout and exception isolation;
- observation `publish` and management queries;
- `flush()` and Gateway shutdown orchestration.

`EventTransport` plugins own only physical delivery:

- in-process queues or cross-process clients;
- serialization and connection management;
- ack, retry, reconnect, and backpressure;
- calling the host dispatcher when an event reaches this process.

The default `in-process` transport uses a bounded asyncio queue. Subscriber
timeout belongs to `config/event.json`; queue size and overflow belong to
`config/plugins/event-transport/in-process.json`.

## Identity

Plugins cannot choose their trusted identity. The host derives it from the
validated manifest:

```python
EventIdentity(
    subject="plugin:listener:timeline",
    kind="plugin",
    name="timeline",
    version="1.0.0",
    package_name=...,
    contribution_id=...,
)
```

The Gateway exposes capability-specific views:

- `EventPublisher`: publish only, with the bound identity written to every
  event;
- `EventSubscriber`: subscribe only, plus a snapshot of that owner's own
  subscriptions;
- `EventGateway.subscriptions()`: management snapshot for host/admin callers;
- transport plugins receive config and lifecycle only, never the routing table.

Plugins should query only their private `PluginContext.config`, host-provided
capabilities, and their own lifecycle state. They must not reach into another
plugin, inspect the complete subscription table, or discover transport
consumers.

## Management queries

Listener plugins declare `Subscription` objects. After the host binds an
`EventSubscriber`, the plugin may inspect `subscriber.subscriptions()` and
`subscriber.subscriber_count(name)`. These calls are scoped to that plugin.

Frontends or administration plugins do not scan plugin directories. They call
`EventGateway.subscriptions()` through a host management API.

## Delivery and shutdown

`publish()` submits an event to the selected transport and does not wait for
subscribers. The transport calls the Gateway dispatcher, which resolves
subscribers, filters scope, applies ordering, and isolates failures.

`EventGateway` has no `decide()` API. Control-plane behavior is synchronous and
belongs to `HookGateway`: `user_prompt_submit` gates one prompt before the
agent loop, and `tool_before` gates each tool call. Observation events such as
`user_prompt.accepted` and `user_prompt.rejected` are published only after the
decision.

```text
Runtime.close()
  -> events.flush()
  -> remove listener subscriptions
  -> lifecycle.stop_all()
  -> event-transport.stop()
```

## Consequences

- A Redis, NATS, or Kafka transport can replace the default without exposing
  its physical details to publishers or listeners.
- Subscription semantics stay consistent across transports.
- Event history, cursors, replay, and projections remain a separate
  EventStore concern.
- Multi-agent message passing remains separate from observation events.
  Task dispatch, acknowledgements, retries, and replies require a future
  MessageBus rather than overloading this gateway.
