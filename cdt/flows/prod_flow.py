import subprocess
import time
from pathlib import Path

import typer

from .. import config
from ..config import _debug_log
from ..platforms.android import (
    _android_aab_artifact,
    _android_apk_artifact,
    _build_android_prod_aab_command,
    _build_android_prod_apk_command,
    copy_artifacts_to_downloads,
)
from ..platforms.ios_flutter import _build_ios_prod_ipa_command, _ios_ipa_artifact
from ..runner import CommandRunner
from ..services.appstore import _upload_testflight
from ..services.notify import _notify_prod_user_agent_pachca, _notify_success
from ..sounds import _play_fail_sound, _play_success_sound
from ..ui import _tracker_set, _tracker_start, _tracker_stop, _ui_echo
from ..versioning import _increment_flutter_build_number


def run_prod_flow(
    cwd: Path,
    env: dict[str, str],
    only: str | None,
    runner: CommandRunner | None = None,
) -> None:
    runner = runner or CommandRunner()
    ios_dir = cwd / "ios"
    android_dir = cwd / "android"

    selected = (only or "").strip().lower()
    if selected not in {"", "ios", "android"}:
        raise typer.BadParameter("Invalid --only value. Use: ios | android")

    run_ios = selected in {"", "ios"}
    run_android = selected in {"", "android"}

    if run_ios and not ios_dir.is_dir():
        typer.echo(f"Missing iOS directory: {ios_dir}", err=True)
        _play_fail_sound(env, cwd)
        raise typer.Exit(code=1)
    if run_android and not android_dir.is_dir():
        typer.echo(f"Missing Android directory: {android_dir}", err=True)
        _play_fail_sound(env, cwd)
        raise typer.Exit(code=1)

    old_version, new_version = _increment_flutter_build_number(cwd)

    _tracker_start()
    _tracker_set("Version", f"{old_version} -> {new_version}")
    if config.UI_MODE != "pretty":
        _ui_echo(f"==> pubspec version bumped: {old_version} -> {new_version}")
    _tracker_set("Flutter deps", "running")

    try:
        prod_pub_get_code = runner.run(["flutter", "pub", "get"], cwd=cwd)
    except KeyboardInterrupt:
        _debug_log("received KeyboardInterrupt (SIGINT) during flutter pub get [prod]")
        _tracker_set("Flutter deps", "interrupted")
        _tracker_set("Result", "interrupted")
        _tracker_stop()
        _ui_echo("⚠️ Прервано на шаге flutter pub get (SIGINT)", err=True)
        raise

    if prod_pub_get_code != 0:
        _tracker_set("Flutter deps", "failed")
        _tracker_set("Result", "failed")
        _tracker_stop()
        _play_fail_sound(env, cwd)
        raise typer.Exit(code=1)
    _tracker_set("Flutter deps", "done")

    ios_build_cmd = _build_ios_prod_ipa_command()
    apk_cmd = _build_android_prod_apk_command()
    aab_cmd = _build_android_prod_aab_command()

    ios_proc: subprocess.Popen | None = None
    apk_proc: subprocess.Popen | None = None
    aab_proc: subprocess.Popen | None = None
    ios_log: Path | None = None
    apk_log: Path | None = None
    aab_log: Path | None = None

    if run_ios:
        _ui_echo("🏗️ Запускаю iOS prod build")
        _tracker_set("iOS prod build", "running")
        ios_spawned = runner.spawn(ios_build_cmd, cwd=cwd)
        ios_proc, ios_log = ios_spawned.proc, ios_spawned.log_path
    if run_android:
        _ui_echo("🏗️ Запускаю Android prod AAB build")
        _tracker_set("Android prod AAB build", "running")
        aab_spawned = runner.spawn(aab_cmd, cwd=cwd)
        aab_proc, aab_log = aab_spawned.proc, aab_spawned.log_path

    ios_done = ios_proc is None
    apk_done = not run_android
    aab_done = aab_proc is None
    apk_pending = run_android
    aab_copied = False
    apk_copied = False

    while not (ios_done and apk_done and aab_done):
        if ios_proc and not ios_done:
            status = ios_proc.poll()
            if status is not None:
                ios_done = True
                if status != 0:
                    _tracker_set("iOS prod build", "failed")
                    _tracker_set("Result", "failed")
                    _ui_echo("❌ iOS prod build failed", err=True)
                    if ios_log:
                        tail = runner.tail(ios_log)
                        if tail:
                            _ui_echo(tail, err=True)
                    for proc in (apk_proc, aab_proc):
                        if proc and proc.poll() is None:
                            proc.terminate()
                    _tracker_stop()
                    _play_fail_sound(env, cwd)
                    raise typer.Exit(code=1)

                _tracker_set("iOS prod build", "done")
                ipa = _ios_ipa_artifact(cwd)
                _tracker_set("TestFlight upload", "running")
                _ui_echo(f"==> Uploading to TestFlight: {ipa.path}")
                if _upload_testflight(ipa.path, env, "prod build", new_version) != 0:
                    _tracker_set("TestFlight upload", "failed")
                    _tracker_set("Result", "failed")
                    _ui_echo("TestFlight upload failed", err=True)
                    for proc in (apk_proc, aab_proc):
                        if proc and proc.poll() is None:
                            proc.terminate()
                    _tracker_stop()
                    _play_fail_sound(env, cwd)
                    raise typer.Exit(code=1)
                _tracker_set("TestFlight upload", "done")

        if apk_proc and not apk_done:
            status = apk_proc.poll()
            if status is not None:
                apk_done = True
                if status != 0:
                    _tracker_set("Android prod APK build", "failed")
                    _tracker_set("Result", "failed")
                    _ui_echo("❌ Android prod APK build failed", err=True)
                    if apk_log:
                        tail = runner.tail(apk_log)
                        if tail:
                            _ui_echo(tail, err=True)
                    for proc in (ios_proc, aab_proc):
                        if proc and proc.poll() is None:
                            proc.terminate()
                    _tracker_stop()
                    _play_fail_sound(env, cwd)
                    raise typer.Exit(code=1)
                _tracker_set("Android prod APK build", "done")
                if not apk_copied:
                    _tracker_set("Android APK copy", "running")
                    apk = _android_apk_artifact(cwd)
                    copy_artifacts_to_downloads([apk])
                    _tracker_set("Android APK copy", "done")
                    apk_copied = True
                    _ui_echo("==> Android APK copied to Downloads")

        if aab_proc and not aab_done:
            status = aab_proc.poll()
            if status is not None:
                aab_done = True
                if status != 0:
                    _tracker_set("Android prod AAB build", "failed")
                    _tracker_set("Result", "failed")
                    _ui_echo("❌ Android prod AAB build failed", err=True)
                    if aab_log:
                        tail = runner.tail(aab_log)
                        if tail:
                            _ui_echo(tail, err=True)
                    for proc in (ios_proc, apk_proc):
                        if proc and proc.poll() is None:
                            proc.terminate()
                    _tracker_stop()
                    _play_fail_sound(env, cwd)
                    raise typer.Exit(code=1)
                _tracker_set("Android prod AAB build", "done")
                if not aab_copied:
                    _tracker_set("Android AAB copy", "running")
                    aab = _android_aab_artifact(cwd)
                    copy_artifacts_to_downloads([aab])
                    _tracker_set("Android AAB copy", "done")
                    aab_copied = True
                    _ui_echo("==> Android AAB copied to Downloads")
                if apk_pending:
                    apk_pending = False
                    _ui_echo("🏗️ Запускаю Android prod APK build")
                    _tracker_set("Android prod APK build", "running")
                    apk_spawned = runner.spawn(apk_cmd, cwd=cwd)
                    apk_proc, apk_log = apk_spawned.proc, apk_spawned.log_path

        if ios_proc and not ios_done:
            _tracker_set("iOS prod build", "running")
        if apk_proc and not apk_done:
            _tracker_set("Android prod APK build", "running")
        if aab_proc and not aab_done:
            _tracker_set("Android prod AAB build", "running")

        if not (ios_done and apk_done and aab_done):
            time.sleep(1)

    _tracker_set("Result", "success")
    _tracker_stop()

    if run_ios and run_android:
        _ui_echo("✅ CDT prod completed: iOS TestFlight + Android artifacts copied to Downloads")
    elif run_ios:
        _ui_echo("✅ CDT prod completed: iOS TestFlight flow")
    else:
        _ui_echo("✅ CDT prod completed: Android artifacts copied to Downloads")

    try:
        _notify_success(env, new_version)
        if env.get("NOTIFY_PROVIDER", "").strip():
            typer.echo("==> Success notification sent")
    except Exception as exc:
        typer.echo(f"⚠️ Notification failed: {exc}", err=True)

    _play_success_sound(env, cwd)

    try:
        _notify_prod_user_agent_pachca(env, new_version)
        if env.get("NOTIFY_PROVIDER", "").strip().lower() == "pachca":
            typer.echo("==> Pachca prod user-agent notification sent")
    except Exception as exc:
        typer.echo(f"⚠️ Pachca prod user-agent notification failed: {exc}", err=True)
