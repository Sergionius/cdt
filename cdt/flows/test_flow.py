import subprocess
import time
from pathlib import Path

import typer

from .. import config
from ..config import _debug_log
from ..platforms.android import _build_android_test_aab_command, _find_android_aab
from ..platforms.ios_flutter import _build_ios_test_ipa_command, _find_ipa
from ..runner import CommandRunner
from ..services.appstore import _build_testflight_transporter_command, _complete_testflight_after_upload
from ..services.firebase import _build_android_apptester_command
from ..services.notify import _notify_success
from ..services.tracker import _tracker_comment
from ..sounds import _play_fail_sound, _play_success_sound
from ..ui import _tracker_set, _tracker_start, _tracker_stop, _ui_echo
from ..versioning import _increment_flutter_build_number


def run_test_flow(
    cwd: Path,
    env: dict[str, str],
    ids: list[str],
    only: str | None,
    runner: CommandRunner | None = None,
) -> None:
    runner = runner or CommandRunner()
    if config.DEBUG_LOG_PATH:
        config.DEBUG_LOG_PATH.write_text("", encoding="utf-8")
    _debug_log("cdt test started")
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
        if config.DEBUG_LOG_PATH:
            _ui_echo(f"🧾 debug log: {config.DEBUG_LOG_PATH}")

    _tracker_set("Flutter deps", "running")
    try:
        pub_get_code = runner.run(["flutter", "pub", "get"], cwd=cwd)
    except KeyboardInterrupt:
        _debug_log("received KeyboardInterrupt (SIGINT) during flutter pub get")
        _tracker_set("Flutter deps", "interrupted")
        _tracker_set("Result", "interrupted")
        _tracker_stop()
        typer.echo("⚠️ Прервано на шаге flutter pub get (SIGINT). Смотри лог: .cdt/last_run.log", err=True)
        raise

    if pub_get_code != 0:
        _tracker_set("Flutter deps", "failed")
        _play_fail_sound(env, cwd)
        _tracker_stop()
        raise typer.Exit(code=1)
    _tracker_set("Flutter deps", "done")

    ios_build_cmd = _build_ios_test_ipa_command()
    android_build_cmd = _build_android_test_aab_command()

    ios_proc: subprocess.Popen | None = None
    android_proc: subprocess.Popen | None = None
    ios_upload_proc: subprocess.Popen | None = None
    android_upload_proc: subprocess.Popen | None = None
    ios_log: Path | None = None
    android_log: Path | None = None
    ios_upload_log: Path | None = None
    android_upload_log: Path | None = None

    try:
        if run_ios and run_android:
            _ui_echo("🏗️ Сборка iOS + Android запущена")
            _tracker_set("iOS build", "running")
            _tracker_set("Android build", "running")
            ios_spawned = runner.spawn(ios_build_cmd, cwd=cwd)
            ios_proc, ios_log = ios_spawned.proc, ios_spawned.log_path
            _debug_log(f"spawn ios pid={ios_proc.pid} log={ios_log}")
            android_spawned = runner.spawn(android_build_cmd, cwd=cwd)
            android_proc, android_log = android_spawned.proc, android_spawned.log_path
            _debug_log(f"spawn android pid={android_proc.pid} log={android_log}")
        elif run_ios:
            _ui_echo("🏗️ Запускаю сборку iOS")
            _tracker_set("iOS build", "running")
            ios_spawned = runner.spawn(ios_build_cmd, cwd=cwd)
            ios_proc, ios_log = ios_spawned.proc, ios_spawned.log_path
            _debug_log(f"spawn ios pid={ios_proc.pid} log={ios_log}")
        else:
            _ui_echo("🏗️ Запускаю сборку Android")
            _tracker_set("Android build", "running")
            android_spawned = runner.spawn(android_build_cmd, cwd=cwd)
            android_proc, android_log = android_spawned.proc, android_spawned.log_path
            _debug_log(f"spawn android pid={android_proc.pid} log={android_log}")

        ios_done = ios_proc is None
        android_done = android_proc is None
        ios_upload_done = not run_ios
        android_upload_done = not run_android
        testflight_finalize_pending = False
        testflight_finalize_done = not run_ios
        test_changelog = f"dev build {', '.join(ids)}" if ids else "dev build"

        while not (ios_done and android_done and ios_upload_done and android_upload_done and testflight_finalize_done):
            if ios_proc and not ios_done:
                ios_status = ios_proc.poll()
                if ios_status is not None:
                    _debug_log(f"ios exited code={ios_status}")
                    ios_done = True
                    if ios_status != 0:
                        _tracker_set("iOS build", "failed")
                        _ui_echo("❌ iOS build failed", err=True)
                        if ios_log:
                            tail = runner.tail(ios_log)
                            if tail:
                                _ui_echo(tail, err=True)
                        if android_proc and not android_done:
                            android_proc.terminate()
                        if android_upload_proc and not android_upload_done:
                            android_upload_proc.terminate()
                        _play_fail_sound(env, cwd)
                        _tracker_stop()
                        raise typer.Exit(code=1)

                    _tracker_set("iOS build", "done")
                    ipa = _find_ipa(cwd)
                    _tracker_set("TestFlight transporter upload", "running")
                    _ui_echo(f"==> Uploading to TestFlight: {ipa}")
                    ios_upload_cmd = _build_testflight_transporter_command(ipa, env)
                    ios_upload_spawned = runner.spawn(ios_upload_cmd, cwd=cwd)
                    ios_upload_proc, ios_upload_log = ios_upload_spawned.proc, ios_upload_spawned.log_path
                    ios_upload_done = False

            if ios_upload_proc and not ios_upload_done:
                ios_upload_status = ios_upload_proc.poll()
                if ios_upload_status is not None:
                    ios_upload_done = True
                    if ios_upload_status != 0:
                        _tracker_set("TestFlight transporter upload", "failed")
                        _ui_echo("TestFlight upload failed", err=True)
                        if ios_upload_log:
                            tail = runner.tail(ios_upload_log)
                            if tail:
                                _ui_echo(tail, err=True)
                        if android_proc and not android_done:
                            android_proc.terminate()
                        if android_upload_proc and not android_upload_done:
                            android_upload_proc.terminate()
                        _play_fail_sound(env, cwd)
                        _tracker_stop()
                        raise typer.Exit(code=1)
                    _tracker_set("TestFlight transporter upload", "done")
                    _tracker_set("App Store Connect", "running")
                    testflight_finalize_pending = True

            if testflight_finalize_pending and not testflight_finalize_done:
                testflight_finalize_pending = False
                try:
                    _complete_testflight_after_upload(env, test_changelog, new_version)
                    _tracker_set("App Store Connect", "done")
                    _tracker_set("TestFlight changelog", "done")
                    testflight_finalize_done = True
                except Exception:
                    _tracker_set("App Store Connect", "failed")
                    _tracker_set("TestFlight changelog", "failed")
                    testflight_finalize_done = True
                    if android_proc and not android_done:
                        android_proc.terminate()
                    if android_upload_proc and not android_upload_done:
                        android_upload_proc.terminate()
                    raise

            if android_proc and not android_done:
                android_status = android_proc.poll()
                if android_status is not None:
                    _debug_log(f"android exited code={android_status}")
                    android_done = True
                    if android_status != 0:
                        _tracker_set("Android build", "failed")
                        _ui_echo("❌ Android AAB build failed", err=True)
                        if android_log:
                            tail = runner.tail(android_log)
                            if tail:
                                _ui_echo(tail, err=True)
                        if ios_proc and not ios_done:
                            ios_proc.terminate()
                        if ios_upload_proc and not ios_upload_done:
                            ios_upload_proc.terminate()
                        _play_fail_sound(env, cwd)
                        _tracker_stop()
                        raise typer.Exit(code=1)

                    _tracker_set("Android build", "done")
                    aab = _find_android_aab(cwd)
                    _tracker_set("AppTester upload", "running")
                    _ui_echo(f"==> Uploading to AppTester (Firebase App Distribution): {aab}")
                    android_upload_cmd = _build_android_apptester_command(aab, env, ids)
                    android_upload_spawned = runner.spawn(android_upload_cmd, cwd=cwd)
                    android_upload_proc, android_upload_log = (
                        android_upload_spawned.proc,
                        android_upload_spawned.log_path,
                    )
                    android_upload_done = False

            if android_upload_proc and not android_upload_done:
                android_upload_status = android_upload_proc.poll()
                if android_upload_status is not None:
                    android_upload_done = True
                    if android_upload_status != 0:
                        _tracker_set("AppTester upload", "failed")
                        _ui_echo("Android AppTester upload failed", err=True)
                        if android_upload_log:
                            tail = runner.tail(android_upload_log)
                            if tail:
                                _ui_echo(tail, err=True)
                        if ios_proc and not ios_done:
                            ios_proc.terminate()
                        if ios_upload_proc and not ios_upload_done:
                            ios_upload_proc.terminate()
                        _play_fail_sound(env, cwd)
                        _tracker_stop()
                        raise typer.Exit(code=1)
                    _tracker_set("AppTester upload", "done")

            if ios_proc and not ios_done:
                _tracker_set("iOS build", "running")
            if android_proc and not android_done:
                _tracker_set("Android build", "running")
            if ios_upload_proc and not ios_upload_done:
                _tracker_set("TestFlight transporter upload", "running")
            if android_upload_proc and not android_upload_done:
                _tracker_set("AppTester upload", "running")
            if not (ios_done and android_done and ios_upload_done and android_upload_done and testflight_finalize_done):
                time.sleep(1)
    except KeyboardInterrupt:
        _debug_log("received KeyboardInterrupt (SIGINT)")
        if ios_proc and ios_proc.poll() is None:
            ios_proc.terminate()
            _debug_log("terminated ios child")
        if android_proc and android_proc.poll() is None:
            android_proc.terminate()
            _debug_log("terminated android child")
        if ios_upload_proc and ios_upload_proc.poll() is None:
            ios_upload_proc.terminate()
            _debug_log("terminated ios upload child")
        if android_upload_proc and android_upload_proc.poll() is None:
            android_upload_proc.terminate()
            _debug_log("terminated android upload child")
        _tracker_set("Result", "interrupted")
        _tracker_stop()
        typer.echo("⚠️ Процесс прерван (SIGINT). Смотри лог: .cdt/last_run.log", err=True)
        raise

    _tracker_set("Result", "success")
    _tracker_stop()

    if run_ios and run_android:
        typer.echo("✅ CDT test completed: Android AppTester + iOS TestFlight upload")
    elif run_ios:
        typer.echo("✅ CDT test completed: iOS TestFlight flow")
    else:
        typer.echo("✅ CDT test completed: Android AppTester flow")

    try:
        _notify_success(env, new_version, ids)
        if env.get("NOTIFY_PROVIDER", "").strip():
            typer.echo("==> Success notification sent")
    except Exception as exc:
        typer.echo(f"⚠️ Notification failed: {exc}", err=True)

    _play_success_sound(env, cwd)

    if ids:
        for issue_id in ids:
            try:
                _tracker_comment(env, issue_id, new_version)
                typer.echo(f"==> Tracker comment added: {issue_id}")
            except Exception as exc:
                typer.echo(f"⚠️ Tracker comment failed for {issue_id}: {exc}", err=True)
