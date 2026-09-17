# transient retry policy

Retries failures classified as `retryable` with exponential backoff. The host
still enforces maximum attempts, maximum delay, total deadline, cancellation,
streaming, and `retry_safe` checks.

Configuration is read from `config/plugins/retry-policy/transient.json`.
