from pathlib import Path

from cdt.platforms.android import (
    _build_android_prod_aab_command,
    _build_android_prod_apk_command,
    _build_android_test_aab_command,
)
from cdt.platforms.web import _build_flutter_web_command
from cdt.services.firebase import _build_firebase_app_distribution_command


def test_android_build_commands():
    assert _build_android_test_aab_command() == [
        "flutter",
        "build",
        "appbundle",
        "--obfuscate",
        "--split-debug-info=obfsymbols",
        "--no-shrink",
        "--no-pub",
    ]
    assert _build_android_prod_aab_command() == [
        "flutter",
        "build",
        "appbundle",
        "--obfuscate",
        "--split-debug-info=obfsymbols",
        "--no-shrink",
        "--dart-define=ENV=prod",
        "--no-pub",
    ]
    assert _build_android_prod_apk_command() == [
        "flutter",
        "build",
        "apk",
        "--obfuscate",
        "--split-debug-info=obfsymbols",
        "--no-shrink",
        "--dart-define=ENV=prod",
        "--dart-define=STORE=ru",
        "--no-pub",
    ]


def test_android_build_command_accepts_custom_flutter_options():
    assert _build_android_test_aab_command(
        dart_defines={"ENV": "qa", "API": "mock"},
        flavor="qa",
        target="lib/main_qa.dart",
        obfuscate=False,
        split_debug_info=None,
        no_shrink=False,
        no_pub=False,
        extra_args=["--build-name=1.2.3"],
    ) == [
        "flutter",
        "build",
        "appbundle",
        "--flavor",
        "qa",
        "--target",
        "lib/main_qa.dart",
        "--dart-define=ENV=qa",
        "--dart-define=API=mock",
        "--build-name=1.2.3",
    ]


def test_android_prod_build_command_merges_default_dart_defines():
    assert _build_android_prod_aab_command(dart_defines={"STORE": "global", "API": "prod"}) == [
        "flutter",
        "build",
        "appbundle",
        "--obfuscate",
        "--split-debug-info=obfsymbols",
        "--no-shrink",
        "--dart-define=ENV=prod",
        "--dart-define=STORE=global",
        "--dart-define=API=prod",
        "--no-pub",
    ]


def test_firebase_app_distribution_command():
    env = {
        "FIREBASE_APP_ID_ANDROID": "android-app-id",
        "FIREBASE_TOKEN": "token",
        "FIREBASE_GROUPS": "qa",
    }

    assert _build_firebase_app_distribution_command(Path("app.aab"), env, ["CDT-1", "CDT-2"]) == [
        "firebase",
        "appdistribution:distribute",
        "app.aab",
        "--app",
        "android-app-id",
        "--groups",
        "qa",
        "--release-notes",
        "https://tracker.yandex.ru/CDT-1\nhttps://tracker.yandex.ru/CDT-2",
        "--token",
        "token",
    ]


def test_flutter_web_command_for_dev_and_prod():
    assert _build_flutter_web_command() == ["flutter", "build", "web", "--release"]
    assert _build_flutter_web_command(env_name="prod")[-1] == "--dart-define=ENV=prod"
