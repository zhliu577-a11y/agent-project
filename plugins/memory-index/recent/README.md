# recent

Derived-memory index that keeps candidates in reverse update-time order.

Use it when recent project state is more important than lexicographic
candidate iteration. The durable records remain owned by `MemoryStore`; this
index can always be rebuilt from them.
