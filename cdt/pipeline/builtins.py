from ..steps.android import AndroidBuildAabStep
from ..steps.appstore import UploadTestFlightStep
from ..steps.firebase import EnsureFirebaseCliStep, FirebaseDeployStep, FirebaseUploadAppDistributionStep
from ..steps.flutter import FlutterPubGetStep
from ..steps.git import GitAddCommitPushStep, PrepareGitMainStep
from ..steps.ios import IncrementIosBuildNumberStep, IosFlutterBuildIpaStep, IosXcodeBuildIpaStep
from ..steps.notify import NotifySuccessStep
from ..steps.tracker import TrackerCommentStep
from ..steps.web import ApplyWebCacheBustingStep, BuildFlutterWebStep, CopyWebBuildStep
from .registry import list_steps, register_step

_BUILTINS: dict[str, type] = {
    "android.build_aab": AndroidBuildAabStep,
    "appstore.upload_testflight": UploadTestFlightStep,
    "firebase.ensure_cli": EnsureFirebaseCliStep,
    "firebase.deploy": FirebaseDeployStep,
    "firebase.upload_app_distribution": FirebaseUploadAppDistributionStep,
    "flutter.pub_get": FlutterPubGetStep,
    "git.commit_push": GitAddCommitPushStep,
    "git.prepare_clean_main": PrepareGitMainStep,
    "ios.bump_xcode_build_number": IncrementIosBuildNumberStep,
    "ios.flutter_build_ipa": IosFlutterBuildIpaStep,
    "ios.xcode_build_ipa": IosXcodeBuildIpaStep,
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
