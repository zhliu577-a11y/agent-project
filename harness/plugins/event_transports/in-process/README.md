# in-process event transport

The default `event-transport.v1` implementation. The host-owned
`EventGateway` resolves subscriptions and policy, while this transport only
moves events through a bounded asyncio queue.

```json
{
  "queueSize": 1024,
  "overflow": "block"
}
```

- `overflow`: `block`, `drop_newest`, or `drop_oldest`.
- Subscriber timeout belongs to `config/event.json`, not to the transport.
