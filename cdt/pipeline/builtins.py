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
from .registry import list_steps, register_step

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


def register_builtin_steps() -> None:
    registered = set(list_steps())
    for name, step_class in _BUILTINS.items():
        if name not in registered:
            register_step(name, step_class)
