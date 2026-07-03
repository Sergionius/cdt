import pytest
import typer

from cdt.flows import deploy_flow
from cdt.pipeline.runner import run_configured_pipeline
from cdt.steps import firebase as firebase_steps
from cdt.steps import git as git_steps
from cdt.steps import notify as notify_steps


class FakeRunner:
    def __init__(self):
        self.runs: list[tuple[list[str], object]] = []

    def run(self, cmd: list[str], *, cwd):
        self.runs.append((cmd, cwd))
        return 0


def test_web_deploy_requires_repository_and_build_place(tmp_path, monkeypatch):
    monkeypatch.setattr(git_steps, "_prepare_git_clean_main", lambda cwd: None)

    with pytest.raises(typer.BadParameter, match="Missing WEB_REPOSITORY or WEB_BUILD_PLACE"):
        deploy_flow.run_web_deploy_flow(tmp_path, {})


def test_firebase_deploy_runs_web_build_then_firebase_deploy(tmp_path, monkeypatch):
    runner = FakeRunner()

    monkeypatch.setattr(firebase_steps, "_ensure_firebase_cli_available", lambda: None)
    monkeypatch.setattr(notify_steps, "_play_success_sound", lambda env, cwd: None)

    deploy_flow.run_firebase_deploy_flow(tmp_path, {}, runner=runner)

    assert runner.runs == [
        (["flutter", "build", "web", "--release"], tmp_path),
        (["firebase", "deploy"], tmp_path),
    ]


def test_web_deploy_success_builds_copies_cache_busts_and_pushes(tmp_path, monkeypatch):
    web_repo = tmp_path / "web-repo"
    web_place = tmp_path / "web-place"
    build_web = tmp_path / "build" / "web"
    web_repo.mkdir()
    build_web.mkdir(parents=True)
    (tmp_path / "pubspec.yaml").write_text("name: app\nversion: 1.2.3+45\n", encoding="utf-8")
    (build_web / "index.html").write_text(
        '<html><script src="flutter_bootstrap.js"></script>\n'
        '<script defer src="main.dart.js" type="application/javascript"></script></html>',
        encoding="utf-8",
    )
    (build_web / "flutter_bootstrap.js").write_text('load("main.dart.js");', encoding="utf-8")
    runner = FakeRunner()
    prepared: list[object] = []

    monkeypatch.setattr(git_steps, "_prepare_git_clean_main", lambda cwd: prepared.append(cwd))
    monkeypatch.setattr(notify_steps, "_play_success_sound", lambda env, cwd: None)

    deploy_flow.run_web_deploy_flow(
        tmp_path,
        {
            "WEB_REPOSITORY": str(web_repo),
            "WEB_BUILD_PLACE": str(web_place),
        },
        runner=runner,
    )

    assert prepared == [tmp_path]
    assert runner.runs == [
        (["flutter", "build", "web", "--release", "--dart-define=ENV=prod"], tmp_path),
        (["git", "add", "."], web_repo),
        (["git", "commit", "-m", "1.2.3+45"], web_repo),
        (["git", "push"], web_repo),
    ]
    assert (web_place / "index.html").exists()
    assert "flutter_bootstrap.js?v=45" in (web_place / "index.html").read_text(encoding="utf-8")
    assert "main.dart.js?v=45" in (web_place / "flutter_bootstrap.js").read_text(encoding="utf-8")


def test_yaml_deploy_pipeline_matches_legacy_web_git_sequence(tmp_path, monkeypatch):
    web_repo = tmp_path / "web-repo"
    web_place = tmp_path / "web-place"
    build_web = tmp_path / "build" / "web"
    web_repo.mkdir()
    build_web.mkdir(parents=True)
    (tmp_path / "pubspec.yaml").write_text("name: app\nversion: 1.2.3+45\n", encoding="utf-8")
    (build_web / "index.html").write_text(
        '<html><script src="flutter_bootstrap.js"></script>\n'
        '<script defer src="main.dart.js" type="application/javascript"></script></html>',
        encoding="utf-8",
    )
    (build_web / "flutter_bootstrap.js").write_text('load("main.dart.js");', encoding="utf-8")
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                f"WEB_REPOSITORY={web_repo}",
                f"WEB_BUILD_PLACE={web_place}",
                "WEB_INNER=false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "cdt.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "",
                "pipelines:",
                "  deploy:",
                "    steps:",
                "      - git.prepare_clean_main",
                "      - web.build:",
                "          env: prod",
                "      - web.copy:",
                "          repository: ${WEB_REPOSITORY}",
                "          destination: ${WEB_BUILD_PLACE}",
                "          inner: ${WEB_INNER}",
                "      - web.cache_bust:",
                "          destination: ${WEB_BUILD_PLACE}",
                "          inner: ${WEB_INNER}",
                "      - git.commit_push:",
                "          repository: ${WEB_REPOSITORY}",
                "          message: ${flutter.version}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    runner = FakeRunner()
    prepared: list[object] = []

    monkeypatch.setattr(git_steps, "_prepare_git_clean_main", lambda cwd: prepared.append(cwd))

    run_configured_pipeline(
        tmp_path,
        {
            "WEB_REPOSITORY": str(web_repo),
            "WEB_BUILD_PLACE": str(web_place),
            "WEB_INNER": "false",
        },
        "deploy",
        runner=runner,
    )

    assert prepared == [tmp_path]
    assert runner.runs == [
        (["flutter", "build", "web", "--release", "--dart-define=ENV=prod"], tmp_path),
        (["git", "add", "."], web_repo),
        (["git", "commit", "-m", "1.2.3+45"], web_repo),
        (["git", "push"], web_repo),
    ]
    assert (web_place / "index.html").exists()
    assert "flutter_bootstrap.js?v=45" in (web_place / "index.html").read_text(encoding="utf-8")
    assert "main.dart.js?v=45" in (web_place / "flutter_bootstrap.js").read_text(encoding="utf-8")
