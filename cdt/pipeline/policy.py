"""Opt-in retry policy for retry-safe pipeline steps.

A configured step may be retried only when both conditions hold:

- its declared ``StepMetadata`` explicitly sets ``retry_safe: True`` (the
  capability is never inferred from ``risk: safe`` or added automatically);
- the attempt failed with :class:`RetryableStepError` raised by the step itself.

A :class:`RetryableStepError` means a transient failure after which the whole
step can run again safely. Before raising it, a step must leave the pipeline
context and its external effects in a state from which rerunning the whole
step is safe. Neither this policy nor the executor performs any generic
rollback of partial effects.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

MAX_ATTEMPTS_LIMIT = 5
MAX_DELAY_SECONDS = 60


class RetryableStepError(Exception):
    """Transient step failure after which rerunning the whole step is safe."""


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded fixed-delay retry policy declared on a leaf step."""

    max_attempts: int = 1
    delay_seconds: float = 0

    @property
    def enabled(self) -> bool:
        return self.max_attempts > 1

    def to_dict(self) -> dict:
        payload: dict = {"max_attempts": self.max_attempts}
        if self.delay_seconds:
            payload["delay_seconds"] = self.delay_seconds
        return payload


def run_with_retry_policy(
    policy: RetryPolicy,
    attempt: Callable[[], None],
    *,
    on_retryable_failure: Callable[[int, RetryableStepError], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run ``attempt`` under the policy; returns the number of attempts made.

    Only ``RetryableStepError`` may trigger another attempt, with a fixed
    delay and a bounded number of attempts. Any other exception, including
    ``BaseException`` subclasses, propagates immediately and is never treated
    as retryable. ``attempt`` must construct a fresh runtime step instance on
    every call.
    """
    attempts = 0
    while True:
        attempts += 1
        try:
            attempt()
        except RetryableStepError as exc:
            if attempts >= policy.max_attempts:
                raise
            if on_retryable_failure is not None:
                on_retryable_failure(attempts, exc)
            if policy.delay_seconds:
                sleep(policy.delay_seconds)
        else:
            return attempts
