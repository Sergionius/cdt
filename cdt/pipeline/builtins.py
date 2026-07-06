from ..steps.android import AndroidBuildAabStep, AndroidBuildApkStep
from ..steps.appstore import UploadTestFlightStep
from ..steps.artifact import CopyArtifactToDownloadsStep
from ..steps.firebase import EnsureFirebaseCliStep, FirebaseDeployStep, FirebaseUploadAppDistributionStep
from ..steps.flutter import FlutterPubGetStep, IncrementFlutterBuildNumberStep
from ..steps.git import GitAddCommitPushStep, PrepareGitMainStep
from ..steps.hook import PythonScriptHookStep
from ..steps.ios import IncrementIosBuildNumberStep, IosFlutterBuildIpaStep, IosXcodeBuildIpaStep
from ..steps.notify import NotifySuccessStep
from ..steps.tracker import TrackerCommentStep
from ..steps.web import ApplyWebCacheBustingStep, BuildFlutterWebStep, CopyWebBuildStep
from .registry import StepMetadata, list_steps, register_step

_BUILTINS: dict[str, type] = {
    "android.build_aab": AndroidBuildAabStep,
    "android.build_apk": AndroidBuildApkStep,
    "appstore.upload_testflight": UploadTestFlightStep,
    "artifact.copy_to_downloads": CopyArtifactToDownloadsStep,
    "firebase.ensure_cli": EnsureFirebaseCliStep,
    "firebase.deploy": FirebaseDeployStep,
    "firebase.upload_app_distribution": FirebaseUploadAppDistributionStep,
    "flutter.increment_build_number": IncrementFlutterBuildNumberStep,
    "flutter.pub_get": FlutterPubGetStep,
    "git.commit_push": GitAddCommitPushStep,
    "git.prepare_clean_main": PrepareGitMainStep,
    "ios.bump_xcode_build_number": IncrementIosBuildNumberStep,
    "ios.flutter_build_ipa": IosFlutterBuildIpaStep,
    "ios.xcode_build_ipa": IosXcodeBuildIpaStep,
    "hook.python_script": PythonScriptHookStep,
    "notify.success": NotifySuccessStep,
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
        produces=("artifact",),
        external_tools=("flutter",),
    ),
    "android.build_apk": StepMetadata(
        name="android.build_apk",
        description="Build an Android APK artifact with Flutter.",
        category="android",
        risk="build",
        produces=("artifact",),
        external_tools=("flutter",),
    ),
    "appstore.upload_testflight": StepMetadata(
        name="appstore.upload_testflight",
        description="Upload an IPA artifact to TestFlight.",
        category="appstore",
        risk="upload",
        requires_artifacts=("artifact",),
        external_tools=("xcrun",),
    ),
    "artifact.copy_to_downloads": StepMetadata(
        name="artifact.copy_to_downloads",
        description="Copy a named artifact to Downloads or another destination.",
        category="artifact",
        risk="artifact",
        requires_artifacts=("artifact",),
        produces=("file",),
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
        requires_artifacts=("artifact",),
        external_tools=("firebase",),
    ),
    "flutter.increment_build_number": StepMetadata(
        name="flutter.increment_build_number",
        description="Increment the Flutter build number in pubspec.yaml.",
        category="flutter",
        risk="artifact",
        produces=("version",),
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
    "ios.bump_xcode_build_number": StepMetadata(
        name="ios.bump_xcode_build_number",
        description="Increment the Xcode iOS build number.",
        category="ios",
        risk="artifact",
        produces=("version",),
        external_tools=("xcrun",),
    ),
    "ios.flutter_build_ipa": StepMetadata(
        name="ios.flutter_build_ipa",
        description="Build an iOS IPA artifact with Flutter.",
        category="ios",
        risk="build",
        produces=("artifact",),
        external_tools=("flutter",),
    ),
    "ios.xcode_build_ipa": StepMetadata(
        name="ios.xcode_build_ipa",
        description="Build an iOS IPA artifact with xcodebuild.",
        category="ios",
        risk="build",
        produces=("artifact",),
        external_tools=("xcodebuild",),
    ),
    "hook.python_script": StepMetadata(
        name="hook.python_script",
        description="Run a project-local Python hook script.",
        category="hook",
        risk="hook",
        external_tools=("python3",),
    ),
    "notify.success": StepMetadata(
        name="notify.success",
        description="Send or play a success notification.",
        category="notify",
        risk="safe",
    ),
    "tracker.comment": StepMetadata(
        name="tracker.comment",
        description="Post a release comment to an issue tracker.",
        category="tracker",
        risk="upload",
    ),
    "web.build": StepMetadata(
        name="web.build",
        description="Build a Flutter web bundle.",
        category="web",
        risk="build",
        produces=("artifact",),
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
        requires_artifacts=("artifact",),
    ),
}


def register_builtin_steps() -> None:
    registered = set(list_steps())
    for name, step_class in _BUILTINS.items():
        if name not in registered:
            register_step(name, step_class, metadata=_BUILTIN_METADATA[name])
