from pathlib import Path

from ..pipeline import PipelineContext, PipelineExecutor
from ..runner import CommandRunner
from ..steps.firebase import EnsureFirebaseCliStep, FirebaseDeployStep
from ..steps.git import GitAddCommitPushStep, PrepareGitMainStep
from ..steps.notify import PlaySuccessSoundStep
from ..steps.web import (
    ApplyWebCacheBustingStep,
    BuildFlutterWebStep,
    CopyWebBuildStep,
    ResolveWebDeployPathsStep,
)


def run_firebase_deploy_flow(
    cwd: Path,
    env: dict[str, str],
    runner: CommandRunner | None = None,
) -> None:
    ctx = PipelineContext(cwd=cwd, env=env, runner=runner or CommandRunner())
    PipelineExecutor().run(
        [
            EnsureFirebaseCliStep(),
            BuildFlutterWebStep(),
            FirebaseDeployStep(),
            PlaySuccessSoundStep(),
        ],
        ctx,
    )


def run_web_deploy_flow(
    cwd: Path,
    env: dict[str, str],
    runner: CommandRunner | None = None,
) -> None:
    ctx = PipelineContext(cwd=cwd, env=env, runner=runner or CommandRunner())
    PipelineExecutor().run(
        [
            PrepareGitMainStep(),
            ResolveWebDeployPathsStep(),
            BuildFlutterWebStep(env_name="prod"),
            CopyWebBuildStep(),
            ApplyWebCacheBustingStep(),
            GitAddCommitPushStep(),
            PlaySuccessSoundStep(),
        ],
        ctx,
    )
