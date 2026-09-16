# tool-metrics listener

`tool-metrics` demonstrates selective observation through `listener.v1`. It
subscribes only to `tool.after` and `tool.denied`, then writes aggregate counts
to `tool-metrics.json` in its own plugin directory.

The listener does not control tool execution. Permission and admission
decisions remain in `HookGateway`.
