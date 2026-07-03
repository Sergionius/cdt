from pathlib import Path

import typer

from ..runner import CommandRunner
from .builtins import register_builtin_steps
from .config import configured_steps, load_pipeline_config, load_plugins
from .context import PipelineContext
from .executor import PipelineExecutor


def run_configured_pipeline(
    cwd: Path,
    env: dict[str, str],
    name: str,
    ids: list[str] | None = None,
    runner: CommandRunner | None = None,
) -> None:
    config = load_pipeline_config(cwd)
    register_builtin_steps()
    load_plugins(config.plugins)
    try:
        pipeline = config.pipelines[name]
    except KeyError as exc:
        available = ", ".join(sorted(config.pipelines)) or "none"
        raise typer.BadParameter(f"Unknown pipeline: {name}. Available pipelines: {available}") from exc
    ctx = PipelineContext(cwd=cwd, env=env, runner=runner or CommandRunner(), ids=ids or [])
    PipelineExecutor().run(configured_steps(pipeline), ctx)
