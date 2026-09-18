# tail-window

Default context policy plugin.

It preserves system instructions and removes the oldest complete message groups
until the estimated model request fits `context.maxTokens`. Assistant tool calls
and their tool results are always removed as one unit.
