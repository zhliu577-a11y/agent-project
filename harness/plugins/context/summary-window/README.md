# summary-window

Alternative context policy plugin.

It reserves part of the model context budget for a deterministic extractive
summary of older messages, then keeps the newest complete message groups
verbatim. Tool calls and their results remain atomic, and the original Session
history is never modified.
