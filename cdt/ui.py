import os
import sys
import time

import typer

from . import config

try:
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
except Exception:  # pragma: no cover
    Console = None
    Live = None
    Table = None

_PRETTY_TRACKER = None


class _PrettyTracker:
    def __init__(self) -> None:
        self.console = Console()
        self.stages: list[tuple[str, str]] = []
        self.stage_started_at: dict[str, float] = {}
        self.stage_duration: dict[str, float] = {}
        self.live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=6,
            auto_refresh=True,
            redirect_stdout=False,
            redirect_stderr=False,
            transient=False,
        )

    def start(self) -> None:
        self.live.start()

    def stop(self) -> None:
        self.live.stop()

    def set(self, stage: str, status: str) -> None:
        now = time.monotonic()

        if status == "running":
            self.stage_started_at.setdefault(stage, now)
            self.stage_duration.pop(stage, None)
        elif status in {"done", "failed", "interrupted", "success"}:
            started = self.stage_started_at.get(stage)
            if started is not None:
                self.stage_duration[stage] = max(0.0, now - started)

        for i, (s, _) in enumerate(self.stages):
            if s == stage:
                self.stages[i] = (stage, status)
                self.live.update(self._render())
                return
        self.stages.append((stage, status))
        self.live.update(self._render())

    def _format_duration(self, seconds: float) -> str:
        total = int(seconds)
        m, s = divmod(total, 60)
        if m:
            return f"{m}m {s}s"
        return f"{s}s"

    def _render(self):
        table = Table(title="CDT · build progress", show_edge=True)
        table.add_column("Stage", style="cyan", no_wrap=True)
        table.add_column("Status", style="white")
        for stage, status in self.stages:
            pretty_status = {
                "running": "⏳ running",
                "done": "✅ done",
                "failed": "❌ failed",
                "interrupted": "⛔ interrupted",
                "success": "✅ success",
            }.get(status, status)
            duration = self.stage_duration.get(stage)
            if duration is not None:
                pretty_status = f"{pretty_status} ({self._format_duration(duration)})"
            elif status == "running" and stage in self.stage_started_at:
                elapsed = time.monotonic() - self.stage_started_at[stage]
                pretty_status = f"{pretty_status} ({self._format_duration(elapsed)})"
            table.add_row(stage, pretty_status)
        return table


def _tracker_start() -> None:
    global _PRETTY_TRACKER
    if config.UI_MODE != "pretty" or Console is None or Live is None or Table is None:
        _PRETTY_TRACKER = None
        return
    if not sys.stdout.isatty() or os.environ.get("TERM", "").lower() == "dumb":
        _PRETTY_TRACKER = None
        return
    _PRETTY_TRACKER = _PrettyTracker()
    _PRETTY_TRACKER.start()


def _tracker_stop() -> None:
    global _PRETTY_TRACKER
    if _PRETTY_TRACKER:
        _PRETTY_TRACKER.stop()
    _PRETTY_TRACKER = None


def _tracker_set(stage: str, status: str) -> None:
    if _PRETTY_TRACKER:
        _PRETTY_TRACKER.set(stage, status)
    elif config.UI_MODE == "pretty":
        typer.echo(f"{stage}: {status}")


def _ui_echo(message: str, *, err: bool = False) -> None:
    if _PRETTY_TRACKER:
        return
    typer.echo(message, err=err)
