from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import typer

from ..pipeline import PipelineContext


class PythonScriptHookStep:
    name = "hook.python_script"

    def __init__(
        self,
        script: str,
        name: str | None = None,
        args: list[str] | None = None,
        env: dict[str, Any] | None = None,
        outputs: list[str] | None = None,
        timeout: int | float | None = 30,
        fail_on_error: bool = True,
        strict_outputs: bool = False,
    ):
        self.hook_name = name or script
        self.script = script
        self.args = args or []
        self.env = env or {}
        self.outputs = outputs or []
        self.timeout = timeout
        self.fail_on_error = fail_on_error
        self.strict_outputs = strict_outputs

    def run(self, ctx: PipelineContext) -> None:
        if not all(isinstance(arg, str) for arg in self.args):
            raise typer.BadParameter("hook.python_script args must be a list of strings")
        script = ctx.project_path(self.script).resolve()
        root = ctx.cwd.resolve()
        try:
            script.relative_to(root)
        except ValueError as exc:
            raise typer.BadParameter(f"hook.python_script script must be inside project root: {script}") from exc
        if not script.exists() or not script.is_file():
            raise typer.BadParameter(f"hook.python_script script not found: {script}")

        before = _tracked_changes(ctx.cwd) if self.strict_outputs else set()
        run_env = dict(os.environ)
        # ctx.env is loaded from .env; shell environment wins.
        for key, value in ctx.env.items():
            run_env.setdefault(key, value)
        for key, value in self.env.items():
            run_env[str(key)] = str(value)

        typer.echo(f"==> Running hook: {self.hook_name}")
        try:
            result = subprocess.run(
                ["python3", str(script), *self.args],
                cwd=ctx.cwd,
                env=run_env,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            if self.fail_on_error:
                raise typer.BadParameter(
                    f"hook.python_script timed out after {self.timeout}s: {self.hook_name}"
                ) from exc
            return

        if result.returncode != 0 and self.fail_on_error:
            raise typer.BadParameter(f"hook.python_script failed with exit code {result.returncode}: {self.hook_name}")

        if self.strict_outputs:
            after = _tracked_changes(ctx.cwd)
            changed = after - before
            allowed = {_normalize_output(path) for path in self.outputs}
            disallowed = sorted(path for path in changed if path not in allowed)
            if disallowed:
                raise typer.BadParameter(
                    "hook.python_script changed tracked files outside outputs: " + ", ".join(disallowed)
                )


def _tracked_changes(cwd: Path) -> set[str]:
    result = subprocess.run(["git", "diff", "--name-only"], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise typer.BadParameter("hook.python_script strict_outputs requires a git repository")
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _normalize_output(path: str) -> str:
    return str(Path(path)).replace("\\", "/")
