# tool-metrics listener

`tool-metrics` demonstrates selective observation through `listener.v1`. It
subscribes only to `tool.after` and `tool.denied`, then writes aggregate counts
to `data/tool-metrics.json` by default. Set `TOOL_METRICS_PATH` to use a
different runtime data location.

The listener does not control tool execution. Permission and admission
decisions remain in `HookGateway`.
