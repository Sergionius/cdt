import pytest
import typer

from cdt.flows import test_flow
from cdt.runner import SpawnedProcess


class FakeProcess:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.pid = 123
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True


class FakeRunner:
    def __init__(self):
        self.runs: list[tuple[list[str], object]] = []
        self.spawns: list[tuple[list[str], object]] = []

    def run(self, command: list[str], *, cwd):
        self.runs.append((command, cwd))
        return 0

    def spawn(self, command: list[str], *, cwd):
        self.spawns.append((command, cwd))
        return SpawnedProcess(proc=FakeProcess(), log_path=None)

    def tail(self, path, lines: int = 60):
        return "fake log"


def _patch_success_side_effects(monkeypatch):
    monkeypatch.setattr(test_flow, "_increment_flutter_build_number", lambda cwd: ("1.0.0+1", "1.0.0+2"))
    monkeypatch.setattr(test_flow, "_notify_success", lambda env, version, ids: None)
    monkeypatch.setattr(test_flow, "_play_success_sound", lambda env, cwd: None)
    monkeypatch.setattr(test_flow, "_play_fail_sound", lambda env, cwd: None)
    monkeypatch.setattr(test_flow, "_tracker_comment", lambda env, issue_id, version: None)


def test_test_flow_rejects_invalid_only(tmp_path):
    with pytest.raises(typer.BadParameter, match="Invalid --only value"):
        test_flow.run_test_flow(tmp_path, {}, [], "web")


def test_test_flow_errors_when_ios_directory_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(test_flow, "_play_fail_sound", lambda env, cwd: None)

    with pytest.raises(typer.Exit) as exc:
        test_flow.run_test_flow(tmp_path, {}, [], "ios")

    assert exc.value.exit_code == 1


def test_test_flow_errors_when_android_directory_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(test_flow, "_play_fail_sound", lambda env, cwd: None)

    with pytest.raises(typer.Exit) as exc:
        test_flow.run_test_flow(tmp_path, {}, [], "android")

    assert exc.value.exit_code == 1


def test_test_flow_android_success_runs_build_and_firebase_upload(tmp_path, monkeypatch):
    (tmp_path / "android").mkdir()
    aab = tmp_path / "build" / "app" / "outputs" / "bundle" / "release" / "app-release.aab"
    aab.parent.mkdir(parents=True)
    aab.write_text("aab", encoding="utf-8")
    runner = FakeRunner()

    _patch_success_side_effects(monkeypatch)
    monkeypatch.setattr(test_flow, "_find_android_aab", lambda cwd: aab)
    monkeypatch.setattr(
        test_flow,
        "_build_android_apptester_command",
        lambda path, env, ids: ["firebase", "appdistribution:distribute", str(path), "--release-notes", ",".join(ids)],
    )

    test_flow.run_test_flow(tmp_path, {}, ["ISSUE-1"], "android", runner=runner)

    assert runner.runs == [(["flutter", "pub", "get"], tmp_path)]
    assert runner.spawns == [
        (
            [
                "flutter",
                "build",
                "appbundle",
                "--obfuscate",
                "--split-debug-info=obfsymbols",
                "--no-shrink",
                "--no-pub",
            ],
            tmp_path,
        ),
        (["firebase", "appdistribution:distribute", str(aab), "--release-notes", "ISSUE-1"], tmp_path),
    ]


def test_test_flow_ios_success_runs_build_upload_and_finalize(tmp_path, monkeypatch):
    (tmp_path / "ios").mkdir()
    ipa = tmp_path / "build" / "ios" / "ipa" / "Runner.ipa"
    runner = FakeRunner()
    finalized: list[tuple[str, str]] = []

    _patch_success_side_effects(monkeypatch)
    monkeypatch.setattr(test_flow, "_find_ipa", lambda cwd: ipa)
    monkeypatch.setattr(
        test_flow,
        "_build_testflight_transporter_command",
        lambda path, env: ["xcrun", "iTMSTransporter", "-assetFile", str(path)],
    )
    monkeypatch.setattr(
        test_flow,
        "_complete_testflight_after_upload",
        lambda env, changelog, version: finalized.append((changelog, version)) or 0,
    )

    test_flow.run_test_flow(tmp_path, {}, ["ISSUE-1", "ISSUE-2"], "ios", runner=runner)

    assert runner.runs == [(["flutter", "pub", "get"], tmp_path)]
    assert runner.spawns == [
        (
            [
                "flutter",
                "build",
                "ipa",
                "--obfuscate",
                "--split-debug-info=obfsymbols",
                "--no-pub",
            ],
            tmp_path,
        ),
        (["xcrun", "iTMSTransporter", "-assetFile", str(ipa)], tmp_path),
    ]
    assert finalized == [("dev build ISSUE-1, ISSUE-2", "1.0.0+2")]


def test_test_flow_both_platforms_spawns_ios_and_android_builds(tmp_path, monkeypatch):
    (tmp_path / "ios").mkdir()
    (tmp_path / "android").mkdir()
    aab = tmp_path / "build" / "app" / "outputs" / "bundle" / "release" / "app-release.aab"
    ipa = tmp_path / "build" / "ios" / "ipa" / "Runner.ipa"
    runner = FakeRunner()

    _patch_success_side_effects(monkeypatch)
    monkeypatch.setattr(test_flow, "_find_android_aab", lambda cwd: aab)
    monkeypatch.setattr(test_flow, "_find_ipa", lambda cwd: ipa)
    monkeypatch.setattr(test_flow, "_build_android_apptester_command", lambda path, env, ids: ["firebase", "upload"])
    monkeypatch.setattr(test_flow, "_build_testflight_transporter_command", lambda path, env: ["xcrun", "upload"])
    monkeypatch.setattr(test_flow, "_complete_testflight_after_upload", lambda env, changelog, version: 0)

    test_flow.run_test_flow(tmp_path, {}, [], None, runner=runner)

    build_commands = [command for command, _cwd in runner.spawns[:2]]
    assert build_commands[0][:3] == ["flutter", "build", "ipa"]
    assert build_commands[1][:3] == ["flutter", "build", "appbundle"]
