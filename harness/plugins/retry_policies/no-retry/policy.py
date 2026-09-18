"""Explicit opt-out retry policy."""

from core.retry import RetryDecision, RetryPolicy, RetryRequest


class NoRetryPolicy(RetryPolicy):
    async def decide(self, request: RetryRequest) -> RetryDecision:
        return RetryDecision(retry=False, reason="retry disabled by policy")


def create_policy(plugin_dir, context=None) -> RetryPolicy:
    del plugin_dir, context
    return NoRetryPolicy()
