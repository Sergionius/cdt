import typer

from ..pipeline import PipelineContext
from ..services.notify import _notify_prod_user_agent_pachca, _notify_success
from ..sounds import _play_success_sound


class NotifySuccessStep:
    name = "notify.success"

    def __init__(self, message: str | None = None, include_ids: bool = False):
        self.message = message
        self.include_ids = include_ids

    def run(self, ctx: PipelineContext) -> None:
        if not ctx.new_version:
            raise typer.BadParameter("Missing pipeline value: new_version")

        typer.echo(f"✅ {self.message}" if self.message else "✅ Pipeline completed")
        issue_ids = ctx.ids if self.include_ids and ctx.ids else None
        try:
            _notify_success(ctx.env, ctx.new_version, issue_ids)
            if ctx.env.get("NOTIFY_PROVIDER", "").strip():
                typer.echo("==> Success notification sent")
        except Exception as exc:
            typer.echo(f"⚠️ Notification failed: {exc}", err=True)


class PlaySuccessSoundStep:
    name = "notify.play_success_sound"

    def run(self, ctx: PipelineContext) -> None:
        _play_success_sound(ctx.env, ctx.cwd)


class NotifyProdUserAgentPachcaStep:
    name = "notify.prod_user_agent"

    def run(self, ctx: PipelineContext) -> None:
        if not ctx.new_version:
            raise typer.BadParameter("Missing pipeline value: new_version")

        try:
            _notify_prod_user_agent_pachca(ctx.env, ctx.new_version)
            if ctx.env.get("NOTIFY_PROVIDER", "").strip().lower() == "pachca":
                typer.echo("==> Pachca prod user-agent notification sent")
        except Exception as exc:
            typer.echo(f"⚠️ Pachca prod user-agent notification failed: {exc}", err=True)
