# inline event transport

`inline` demonstrates an `event-transport.v1` implementation that calls the
host dispatcher immediately in the publishing task. It has no background
queue, so a successful `publish()` means local subscribers have already run.

Select it with:

```json
{
  "provider": "inline"
}
```
