# strict

Policy for memory deployments that prefer precision over recall.

Writes shorter than eight characters, low-confidence records, low-importance
records, expired records, and non-active records are rejected. Recall ranks
confidence before importance and recency.
