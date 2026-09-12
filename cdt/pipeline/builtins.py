from ..steps.android import AndroidBuildAabStep, AndroidBuildApkStep
from ..steps.appstore import CompleteTestFlightStep, UploadTestFlightIpaStep, UploadTestFlightStep
from ..steps.artifact import CopyArtifactToDownloadsStep
from ..steps.firebase import EnsureFirebaseCliStep, FirebaseDeployStep, FirebaseUploadAppDistributionStep
from ..steps.flutter import FlutterPubGetStep, IncrementFlutterBuildNumberStep
from ..steps.git import (
    GitAddCommitPushStep,
    PrepareGitMainStep,
    ReleaseCommitStep,
    ReleaseTagPushStep,
    RequireSyncedMainStep,
)
from ..steps.github import WaitReleaseStep
from ..steps.hook import PythonScriptHookStep
from ..steps.ios import IncrementIosBuildNumberStep, IosFlutterBuildIpaStep, IosXcodeBuildIpaStep
from ..steps.notify import NotifyProdUserAgentPachcaStep, NotifySuccessStep
from ..steps.python import BuildDistributionStep, PrepareReleaseStep, PytestStep, RuffCheckStep
from ..steps.release import RequireVersionAvailableStep
from ..steps.tracker import TrackerCommentStep
from ..steps.web import ApplyWebCacheBustingStep, BuildFlutterWebStep, CopyWebBuildStep
from .registry import ResultProduction, ResultRequirement, StepMetadata, list_steps, register_step

_BUILTINS: dict[str, type] = {
    "android.build_aab": AndroidBuildAabStep,
    "android.build_apk": AndroidBuildApkStep,
    "appstore.complete_testflight": CompleteTestFlightStep,
    "appstore.upload_testflight": UploadTestFlightStep,
    "appstore.upload_testflight_ipa": UploadTestFlightIpaStep,
    "artifact.copy_to_downloads": CopyArtifactToDownloadsStep,
    "firebase.ensure_cli": EnsureFirebaseCliStep,
    "firebase.deploy": FirebaseDeployStep,
    "firebase.upload_app_distribution": FirebaseUploadAppDistributionStep,
    "flutter.increment_build_number": IncrementFlutterBuildNumberStep,
    "flutter.pub_get": FlutterPubGetStep,
    "git.commit_push": GitAddCommitPushStep,
    "git.prepare_clean_main": PrepareGitMainStep,
    "git.release_commit": ReleaseCommitStep,
    "git.release_tag_push": ReleaseTagPushStep,
    "git.require_synced_main": RequireSyncedMainStep,
    "github.wait_release": WaitReleaseStep,
    "ios.bump_xcode_build_number": IncrementIosBuildNumberStep,
    "ios.flutter_build_ipa": IosFlutterBuildIpaStep,
    "ios.xcode_build_ipa": IosXcodeBuildIpaStep,
    "hook.python_script": PythonScriptHookStep,
    "notify.prod_user_agent": NotifyProdUserAgentPachcaStep,
    "notify.success": NotifySuccessStep,
    "python.build_distribution": BuildDistributionStep,
    "python.prepare_release": PrepareReleaseStep,
    "python.pytest": PytestStep,
    "python.ruff_check": RuffCheckStep,
    "release.require_version_available": RequireVersionAvailableStep,
    "tracker.comment": TrackerCommentStep,
    "web.build": BuildFlutterWebStep,
    "web.cache_bust": ApplyWebCacheBustingStep,
    "web.copy": CopyWebBuildStep,
}

_BUILTIN_METADATA: dict[str, StepMetadata] = {
    "android.build_aab": StepMetadata(
        name="android.build_aab",
        description="Build an Android App Bundle artifact with Flutter.",
        category="android",
        risk="build",
        produces=(ResultProduction("android_aab", name_options=("artifact",)),),
        external_tools=("flutter",),
    ),
    "android.build_apk": StepMetadata(
        name="android.build_apk",
        description="Build an Android APK artifact with Flutter.",
        category="android",
        risk="build",
        produces=(ResultProduction("android_apk", name_options=("artifact",)),),
        external_tools=("flutter",),
    ),
    "appstore.upload_testflight": StepMetadata(
        name="appstore.upload_testflight",
        description="Upload an IPA artifact to TestFlight.",
        category="appstore",
        risk="upload",
        requires=(ResultRequirement(("ios_ipa",), name_options=("artifact",)),),
        produces=(ResultProduction("upload_result"),),
        external_tools=("xcrun",),
        requires_env=("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY_PATH", "IOS_BUNDLE_ID"),
    ),
    "appstore.upload_testflight_ipa": StepMetadata(
        name="appstore.upload_testflight_ipa",
        description=(
            "Upload an IPA artifact to TestFlight with iTMSTransporter only, "
            "without post-upload ASC processing."
        ),
        category="appstore",
        risk="upload",
        requires=(ResultRequirement(("ios_ipa",), name_options=("artifact",)),),
        produces=(ResultProduction("upload_result"),),
        external_tools=("xcrun",),
        requires_env=("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY_PATH"),
    ),
    "appstore.complete_testflight": StepMetadata(
        name="appstore.complete_testflight",
        description=(
            "Find an already uploaded TestFlight build, wait for terminal processing "
            "and idempotently set the changelog. Requires the pipeline version context "
            "(new_version); does not need an artifact or iTMSTransporter."
        ),
        category="appstore",
        risk="upload",
        requires_env=("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY_PATH", "IOS_BUNDLE_ID"),
    ),
    "artifact.copy_to_downloads": StepMetadata(
        name="artifact.copy_to_downloads",
        description="Copy a named artifact to Downloads or another destination.",
        category="artifact",
        risk="artifact",
        requires=(ResultRequirement(("artifact",), name_options=("artifact",)),),
        produces=(ResultProduction("file"),),
    ),
    "firebase.ensure_cli": StepMetadata(
        name="firebase.ensure_cli",
        description="Check that the Firebase CLI is available.",
        category="firebase",
        risk="safe",
        external_tools=("firebase",),
    ),
    "firebase.deploy": StepMetadata(
        name="firebase.deploy",
        description="Deploy a Firebase project target.",
        category="firebase",
        risk="deploy",
        external_tools=("firebase",),
    ),
    "firebase.upload_app_distribution": StepMetadata(
        name="firebase.upload_app_distribution",
        description="Upload an app artifact to Firebase App Distribution.",
        category="firebase",
        risk="upload",
        requires=(
            ResultRequirement(
                ("android_aab", "android_apk"),
                mode="any",
                name_options=("artifact",),
            ),
        ),
        produces=(ResultProduction("upload_result"),),
        external_tools=("firebase",),
        requires_env=("FIREBASE_APP_ID_ANDROID", "FIREBASE_TOKEN"),
    ),
    "flutter.increment_build_number": StepMetadata(
        name="flutter.increment_build_number",
        description="Increment the Flutter build number in pubspec.yaml.",
        category="flutter",
        risk="artifact",
        produces=(ResultProduction("version"),),
    ),
    "flutter.pub_get": StepMetadata(
        name="flutter.pub_get",
        description="Run flutter pub get.",
        category="flutter",
        risk="safe",
        external_tools=("flutter",),
    ),
    "git.commit_push": StepMetadata(
        name="git.commit_push",
        description="Commit and push changes with Git.",
        category="git",
        risk="push",
        external_tools=("git",),
    ),
    "git.prepare_clean_main": StepMetadata(
        name="git.prepare_clean_main",
        description="Prepare a clean main branch before release work.",
        category="git",
        risk="safe",
        external_tools=("git",),
    ),
    "git.release_commit": StepMetadata(
        name="git.release_commit",
        description=(
            "Stage only the explicitly configured release files, refuse unrelated staged changes, create the "
            "'Release vX.Y.Z' commit, verify its contents and close the pre-commit rollback boundary."
        ),
        category="git",
        risk="release",
        external_tools=("git",),
    ),
    "git.release_tag_push": StepMetadata(
        name="git.release_tag_push",
        description=(
            "Create an annotated tag on the release commit, reject conflicting local/remote tags and atomically "
            "push the main branch together with the tag. Resumable: an existing matching tag is reused instead "
            "of being moved."
        ),
        category="git",
        risk="push",
        external_tools=("git",),
    ),
    "git.require_synced_main": StepMetadata(
        name="git.require_synced_main",
        description=(
            "Fetch origin/main and tags, then require a clean tracked working tree, the configured main branch, "
            "a configured remote and exact equality of local HEAD and origin/main."
        ),
        category="git",
        risk="safe",
        external_tools=("git",),
    ),
    "github.wait_release": StepMetadata(
        name="github.wait_release",
        description=(
            "Wait via machine-readable gh output for the GitHub Actions release workflow run of the exact "
            "release tag, require a green terminal conclusion (otherwise fail with the run URL and failed job "
            "summary), then verify the GitHub Release is not draft/prerelease and ships wheel, sdist and "
            "SHA256SUMS assets, and confirm the exact version with wheel/sdist files on PyPI (bounded "
            "retries for indexing delay and transient failures). Registers the confirmed GitHub Release/PyPI "
            "URLs and results in context/status. Classifies failures as timeout, transient, workflow_failed "
            "or release_incomplete for the agent; never reruns the workflow automatically."
        ),
        category="github",
        risk="safe",
        produces=(ResultProduction("release_confirmation"),),
        external_tools=("gh",),
    ),
    "ios.bump_xcode_build_number": StepMetadata(
        name="ios.bump_xcode_build_number",
        description="Increment the Xcode iOS build number.",
        category="ios",
        risk="artifact",
        produces=(ResultProduction("version"),),
        external_tools=("xcrun",),
    ),
    "ios.flutter_build_ipa": StepMetadata(
        name="ios.flutter_build_ipa",
        description="Build an iOS IPA artifact with Flutter.",
        category="ios",
        risk="build",
        produces=(ResultProduction("ios_ipa", name_options=("artifact",)),),
        external_tools=("flutter",),
    ),
    "ios.xcode_build_ipa": StepMetadata(
        name="ios.xcode_build_ipa",
        description="Build an iOS IPA artifact with xcodebuild.",
        category="ios",
        risk="build",
        produces=(ResultProduction("ios_ipa", name_options=("artifact",)),),
        external_tools=("xcodebuild",),
    ),
    "hook.python_script": StepMetadata(
        name="hook.python_script",
        description="Run a project-local Python hook script.",
        category="hook",
        risk="hook",
        external_tools=("python3",),
    ),
    "notify.prod_user_agent": StepMetadata(
        name="notify.prod_user_agent",
        description="Send production user-agent details to Pachca when Pachca notifications are enabled.",
        category="notify",
        risk="upload",
        produces=(ResultProduction("notification"),),
        requires_env=("PACHCA_USER_AGENT_WEBHOOK_URL", "UA_APP_NAME"),
    ),
    "notify.success": StepMetadata(
        name="notify.success",
        description="Send or play a success notification.",
        category="notify",
        risk="safe",
        produces=(ResultProduction("notification"),),
    ),
    "python.build_distribution": StepMetadata(
        name="python.build_distribution",
        description=(
            "Remove only the configured dist/ directory, build with the current Python (-m build), verify the "
            "results with -m twine check and register the wheel and sdist as artifacts."
        ),
        category="python",
        risk="build",
        produces=(
            ResultProduction("python_wheel", name_options=("wheel_artifact",)),
            ResultProduction("python_sdist", name_options=("sdist_artifact",)),
        ),
    ),
    "python.prepare_release": StepMetadata(
        name="python.prepare_release",
        description=(
            "Prepare Python release files: bump the pyproject/version-file versions, fold the real Unreleased "
            "changelog entries into a dated '## vX.Y.Z - YYYY-MM-DD' section, update tag-reference files and "
            "snapshot every modified file for pre-commit rollback."
        ),
        category="python",
        risk="artifact",
        produces=(ResultProduction("version"),),
    ),
    "python.pytest": StepMetadata(
        name="python.pytest",
        description="Run the approved pytest command: pytest -q.",
        category="python",
        risk="safe",
        external_tools=("pytest",),
    ),
    "python.ruff_check": StepMetadata(
        name="python.ruff_check",
        description="Run the approved lint command: ruff check .",
        category="python",
        risk="safe",
        external_tools=("ruff",),
    ),
    "release.require_version_available": StepMetadata(
        name="release.require_version_available",
        description=(
            "Preflight the explicit release version: it must be explicit semver, strictly newer than the package "
            "version and absent from the changelog, local/remote git tags, GitHub Releases (via gh) and PyPI "
            "(public JSON API with bounded retries)."
        ),
        category="release",
        risk="safe",
        produces=(ResultProduction("version"),),
        external_tools=("git", "gh"),
    ),
    "tracker.comment": StepMetadata(
        name="tracker.comment",
        description="Post a release comment to an issue tracker.",
        category="tracker",
        risk="upload",
        produces=(ResultProduction("tracker_comment"),),
        requires_env=("TRACKER_OAUTH_TOKEN", "TRACKER_ORG_ID"),
    ),
    "web.build": StepMetadata(
        name="web.build",
        description="Build a Flutter web bundle.",
        category="web",
        risk="build",
        produces=(ResultProduction("web_build"),),
        external_tools=("flutter",),
    ),
    "web.cache_bust": StepMetadata(
        name="web.cache_bust",
        description="Apply cache-busting changes to a web build.",
        category="web",
        risk="artifact",
    ),
    "web.copy": StepMetadata(
        name="web.copy",
        description="Copy a web build to its deployment location.",
        category="web",
        risk="deploy",
        produces=(ResultProduction("file"),),
    ),
}


def register_builtin_steps() -> None:
    registered = set(list_steps())
    for name, step_class in _BUILTINS.items():
        if name not in registered:
            register_step(name, step_class, metadata=_BUILTIN_METADATA[name])
