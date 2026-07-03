from pathlib import Path

from ..pipeline import PipelineContext, PipelineExecutor
from ..runner import CommandRunner
from ..steps.appstore import UploadTestFlightStep
from ..steps.ios import IncrementIosBuildNumberStep, IosXcodeBuildIpaStep
from ..steps.notify import NotifyProdUserAgentPachcaStep, NotifySuccessStep, PlaySuccessSoundStep
from ..steps.tracker import TrackerCommentStep


def _run_ios_flow(
    cwd: Path,
    env: dict[str, str],
    scheme_key: str,
    fallback_scheme_key: str,
    mode: str,
    changelog: str,
    ids: list[str] | None = None,
    notify_prod_user_agent: bool = False,
) -> None:
    issue_ids = ids or []
    command_name = "ios-test" if mode == "test" else "ios-prod"
    ctx = PipelineContext(cwd=cwd, env=env, runner=CommandRunner(), ids=issue_ids)
    steps = [
        IncrementIosBuildNumberStep(scheme_key, fallback_scheme_key),
        IosXcodeBuildIpaStep(mode),
        UploadTestFlightStep(changelog),
        NotifySuccessStep(command_name, include_ids=True),
        PlaySuccessSoundStep(),
    ]
    if issue_ids:
        steps.append(TrackerCommentStep())
    if notify_prod_user_agent:
        steps.append(NotifyProdUserAgentPachcaStep())

    PipelineExecutor().run(steps, ctx)


def run_ios_test_flow(cwd: Path, env: dict[str, str], ids: list[str]) -> None:
    changelog = f"dev build {', '.join(ids)}" if ids else "dev build"
    _run_ios_flow(cwd, env, "IOS_TEST_SCHEME", "NATIVE_TEST_SCHEME", "test", changelog, ids=ids)


def run_ios_prod_flow(cwd: Path, env: dict[str, str]) -> None:
    _run_ios_flow(
        cwd,
        env,
        "IOS_PROD_SCHEME",
        "NATIVE_PROD_SCHEME",
        "prod",
        "prod build",
        notify_prod_user_agent=True,
    )
