import pytest
import typer

from cdt.artifacts import ArtifactKind, BuildArtifact
from cdt.flows import prod_flow
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
    def __init__(self, spawn_returncodes: list[int] | None = None):
        self.runs: list[tuple[list[str], object]] = []
        self.spawns: list[tuple[list[str], object]] = []
        self._spawn_returncodes = list(spawn_returncodes or [])

    def run(self, command: list[str], *, cwd):
        self.runs.append((command, cwd))
        return 0

    def spawn(self, command: list[str], *, cwd):
        self.spawns.append((command, cwd))
        returncode = self._spawn_returncodes.pop(0) if self._spawn_returncodes else 0
        return SpawnedProcess(proc=FakeProcess(returncode), log_path=None)

    def tail(self, path, lines: int = 60):
        return "fake log"


def _patch_success_side_effects(monkeypatch):
    monkeypatch.setattr(prod_flow, "_increment_flutter_build_number", lambda cwd: ("1.0.0+1", "1.0.0+2"))
    monkeypatch.setattr(prod_flow, "_notify_success", lambda env, version: None)
    monkeypatch.setattr(prod_flow, "_notify_prod_user_agent_pachca", lambda env, version: None)
    monkeypatch.setattr(prod_flow, "_play_success_sound", lambda env, cwd: None)
    monkeypatch.setattr(prod_flow, "_play_fail_sound", lambda env, cwd: None)


def test_prod_flow_rejects_invalid_only(tmp_path):
    with pytest.raises(typer.BadParameter, match="Invalid --only value"):
        prod_flow.run_prod_flow(tmp_path, {}, "web")


def test_prod_flow_errors_when_ios_directory_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(prod_flow, "_play_fail_sound", lambda env, cwd: None)

    with pytest.raises(typer.Exit) as exc:
        prod_flow.run_prod_flow(tmp_path, {}, "ios")

    assert exc.value.exit_code == 1


def test_prod_flow_errors_when_android_directory_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(prod_flow, "_play_fail_sound", lambda env, cwd: None)

    with pytest.raises(typer.Exit) as exc:
        prod_flow.run_prod_flow(tmp_path, {}, "android")

    assert exc.value.exit_code == 1


def test_prod_flow_android_success_builds_aab_then_apk_and_copies(tmp_path, monkeypatch):
    (tmp_path / "android").mkdir()
    aab = tmp_path / "build" / "app" / "outputs" / "bundle" / "release" / "app-release.aab"
    apk = tmp_path / "build" / "app" / "outputs" / "flutter-apk" / "app-release.apk"
    aab.parent.mkdir(parents=True)
    apk.parent.mkdir(parents=True)
    aab.write_text("aab", encoding="utf-8")
    apk.write_text("apk", encoding="utf-8")
    copied: list[list[str]] = []
    runner = FakeRunner()

    _patch_success_side_effects(monkeypatch)
    monkeypatch.setattr(
        prod_flow,
        "copy_artifacts_to_downloads",
        lambda artifacts: copied.append([artifact.label for artifact in artifacts]),
    )

    prod_flow.run_prod_flow(tmp_path, {}, "android", runner=runner)

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
                "--dart-define=ENV=prod",
                "--no-pub",
            ],
            tmp_path,
        ),
        (
            [
                "flutter",
                "build",
                "apk",
                "--obfuscate",
                "--split-debug-info=obfsymbols",
                "--no-shrink",
                "--dart-define=ENV=prod",
                "--dart-define=STORE=ru",
                "--no-pub",
            ],
            tmp_path,
        ),
    ]
    assert copied == [["Android AAB"], ["Android APK"]]


def test_prod_flow_ios_success_builds_and_uploads_testflight(tmp_path, monkeypatch):
    (tmp_path / "ios").mkdir()
    ipa = tmp_path / "build" / "ios" / "ipa" / "Runner.ipa"
    uploads: list[tuple[object, str, str]] = []
    runner = FakeRunner()

    _patch_success_side_effects(monkeypatch)
    monkeypatch.setattr(
        prod_flow,
        "_ios_ipa_artifact",
        lambda cwd: BuildArtifact(kind=ArtifactKind.IPA, path=ipa, label="iOS IPA"),
    )
    monkeypatch.setattr(
        prod_flow,
        "_upload_testflight",
        lambda path, env, changelog, version: uploads.append((path, changelog, version)) or 0,
    )

    prod_flow.run_prod_flow(tmp_path, {}, "ios", runner=runner)

    assert runner.runs == [(["flutter", "pub", "get"], tmp_path)]
    assert runner.spawns == [
        (
            [
                "flutter",
                "build",
                "ipa",
                "--obfuscate",
                "--split-debug-info=obfsymbols",
                "--dart-define=ENV=prod",
                "--no-pub",
            ],
            tmp_path,
        )
    ]
    assert uploads == [(ipa, "prod build", "1.0.0+2")]


def test_prod_flow_android_aab_failure_exits_before_apk(tmp_path, monkeypatch):
    (tmp_path / "android").mkdir()
    runner = FakeRunner(spawn_returncodes=[1])
    copied: list[object] = []

    _patch_success_side_effects(monkeypatch)
    monkeypatch.setattr(prod_flow, "copy_artifacts_to_downloads", lambda artifacts: copied.append(artifacts))

    with pytest.raises(typer.Exit) as exc:
        prod_flow.run_prod_flow(tmp_path, {}, "android", runner=runner)

    assert exc.value.exit_code == 1
    assert len(runner.spawns) == 1
    assert runner.spawns[0][0][:3] == ["flutter", "build", "appbundle"]
    assert copied == []
