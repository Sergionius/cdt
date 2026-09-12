import typer

from ..pipeline import PipelineContext
from ..runner import CommandExecutionError
from ..services.firebase import _build_firebase_app_distribution_command, _ensure_firebase_cli_available
from ..sounds import _play_fail_sound


class EnsureFirebaseCliStep:
    name = "firebase.ensure_cli"

    def run(self, ctx: PipelineContext) -> None:
        _ensure_firebase_cli_available()


class FirebaseDeployStep:
    name = "firebase.deploy"

    def run(self, ctx: PipelineContext) -> None:
        if ctx.runner.run(["firebase", "deploy"], cwd=ctx.cwd) != 0:
            _play_fail_sound(ctx.env, ctx.cwd)
            raise typer.Exit(code=1)
        typer.echo("✅ Firebase deploy completed")


class FirebaseUploadAppDistributionStep:
    name = "firebase.upload_app_distribution"

    def __init__(self, artifact: str = "aab", release_notes_from_ids: bool = False):
        self.artifact = artifact
        self.release_notes_from_ids = release_notes_from_ids

    def run(self, ctx: PipelineContext) -> None:
        aab = ctx.artifact(self.artifact)
        ids = ctx.ids if self.release_notes_from_ids else []
        command = _build_firebase_app_distribution_command(aab.path, ctx.env, ids)
        exit_code = ctx.runner.run(command, cwd=ctx.cwd)
        if exit_code != 0:
            _play_fail_sound(ctx.env, ctx.cwd)
            raise CommandExecutionError(
                "Firebase App Distribution upload failed. Check the Firebase CLI output above for details; "
                "inspect the saved run with cdt logs <run-id>.",
                command=command,
                exit_code=exit_code,
            )
        typer.echo("✅ Firebase App Distribution upload completed")
