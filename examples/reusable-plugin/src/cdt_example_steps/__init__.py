"""Example package with reusable CDT pipeline steps.

This package demonstrates the supported plugin mechanism: an ordinary Python
package whose module registers steps through the :mod:`cdt.sdk` decorator into
the same registry as the built-ins. Importing the module performs the
registration; CDT discovers and installs nothing automatically.

Steps here are read-only examples without real network actions. The
``example.check_file`` step declares the explicit ``retry_safe`` capability:
it performs no writes and no external effects, so rerunning the whole step is
always safe, and ``retry: {max_attempts: >1}`` is accepted in ``cdt.yaml``.
"""

from __future__ import annotations

from cdt.sdk import RetryableStepError, step


@step(
    "example.check_file",
    description="Read-only probe that records an existing project file path in pipeline values.",
    category="example",
    risk="safe",
    retry_safe=True,
)
def check_file(ctx, path: str = "pubspec.yaml"):
    """Check that ``path`` exists in the project and remember it.

    The step performs no writes and no network actions; the only effect is the
    ``example_checked_file`` value. A missing file is reported as a retryable
    transient failure: raising ``RetryableStepError`` tells the retry policy
    that rerunning the whole step is safe. Use it only for genuinely transient
    failures of side-effect-free steps — never wrap ambiguous mutations.
    """
    resolved = ctx.project_path(path)
    if not resolved.is_file():
        raise RetryableStepError(f"Project file does not exist (yet): {path}")
    ctx.values["example_checked_file"] = str(resolved)
