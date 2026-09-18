# memory-tail-window

Tail-window context policy with an optional automatic memory contribution.

`ContextGateway` recalls records for the latest user message and exposes them
as `request.state["memory.records"]`. This policy reserves a bounded part of
the model context for those records, renders them as reference data, and then
keeps the newest complete conversation groups.

The default memory budget ratio is `0.25` and can be changed in
`config/plugins/context/memory-tail-window.json`.
