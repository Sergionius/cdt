import typer

from ..pipeline import PipelineContext
from ..services.tracker import _tracker_comment


class TrackerCommentStep:
    name = "tracker.comment"

    def run(self, ctx: PipelineContext) -> None:
        if not ctx.new_version:
            raise typer.BadParameter("Missing pipeline value: new_version")

        for issue_id in ctx.ids:
            try:
                _tracker_comment(ctx.env, issue_id, ctx.new_version)
                typer.echo(f"==> Tracker comment added: {issue_id}")
            except Exception as exc:
                typer.echo(f"⚠️ Tracker comment failed for {issue_id}: {exc}", err=True)
