import urllib.error

from typer.testing import CliRunner

from cdt.cli import app

runner = CliRunner()


def test_doctor_reports_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cdt.yaml").write_text("version: 1\npipelines:\n  test:\n    steps: []\n", encoding="utf-8")
    monkeypatch.setattr("cdt.doctor._detect_install_method", lambda: ("pipx", False))
    monkeypatch.setattr("cdt.doctor.shutil.which", lambda name: "/usr/bin/pipx" if name == "pipx" else None)

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response())

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "python" in result.output
    assert "cdt_yaml_valid" in result.output


def test_doctor_uses_github_token(monkeypatch):
    captured = {}
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, **kwargs):
        captured["authorization"] = request.get_header("Authorization")
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = runner.invoke(app, ["doctor"], catch_exceptions=False)

    assert captured["authorization"] == "Bearer secret-token"
    assert "github_api" in result.output


def test_doctor_fails_on_invalid_yaml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cdt.yaml").write_text("version: [\n", encoding="utf-8")
    monkeypatch.setattr("cdt.doctor._detect_install_method", lambda: None)
    monkeypatch.setattr("cdt.doctor.shutil.which", lambda name: None)
    def fail_urlopen(*args, **kwargs):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr("urllib.request.urlopen", fail_urlopen)

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code != 0
    assert "cdt_yaml_valid" in result.output
    assert "YAML parse error" in result.output
