import re
from pathlib import Path

from cdt.pipeline.builtins import register_builtin_steps
from cdt.pipeline.config import load_pipeline_config
from cdt.pipeline.registry import _clear_steps_for_tests
from cdt.pipeline.validation import validate_pipeline

ROOT = Path(__file__).resolve().parents[1]

RELEASE_STEP_ORDER = [
    "git.require_synced_main",
    "release.require_version_available",
    "python.ruff_check",
    "python.pytest",
    "python.prepare_release",
    "python.build_distribution",
    "git.release_commit",
    "git.release_tag_push",
    "github.wait_release",
]

RELEASE_FILES = [
    "pyproject.toml",
    "cdt/__init__.py",
    "CHANGELOG.md",
    "README.md",
    "docs/getting-started.md",
]

TAG_REFERENCE_REGEX = r"(git\+https://github\.com/Sergionius/cdt\.git@)v\d+\.\d+\.\d+(?:[.-][A-Za-z0-9]+)?"


def setup_function():
    _clear_steps_for_tests()


def _release_pipeline():
    config = load_pipeline_config(ROOT)
    register_builtin_steps()
    assert validate_pipeline(config) == []
    return config.pipelines["release"]


def test_install_docs_use_current_release_tag():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'^version = "([^"]+)"', pyproject, flags=re.MULTILINE).group(1)
    expected = f"git+https://github.com/Sergionius/cdt.git@v{version}"

    for path in (ROOT / "README.md", ROOT / "docs" / "getting-started.md"):
        text = path.read_text(encoding="utf-8")
        assert expected in text
        assert "git+https://github.com/Sergionius/cdt.git@v0.3.3" not in text


def test_legacy_release_script_is_removed():
    assert not (ROOT / "scripts" / "release.py").exists()


def test_repository_declares_valid_production_release_pipeline():
    config = load_pipeline_config(ROOT)
    register_builtin_steps()

    assert validate_pipeline(config) == []
    pipeline = config.pipelines["release"]
    assert pipeline.risk == "production"


def test_release_pipeline_requires_explicit_semver_version_input():
    pipeline = _release_pipeline()

    assert set(pipeline.inputs) == {"version"}
    assert pipeline.inputs["version"].required
    assert pipeline.inputs["version"].pattern is not None
    assert re.fullmatch(pipeline.inputs["version"].pattern, "0.5.2")
    assert re.fullmatch(pipeline.inputs["version"].pattern, "0.5.2-rc1")
    assert re.fullmatch(pipeline.inputs["version"].pattern, "v0.5.2") is None
    assert re.fullmatch(pipeline.inputs["version"].pattern, "0.5") is None


def test_release_pipeline_has_safe_step_order():
    pipeline = _release_pipeline()

    names = [step.name for step in pipeline.steps]
    assert names == RELEASE_STEP_ORDER


def test_release_pipeline_preflights_version_before_any_mutation():
    pipeline = _release_pipeline()

    names = [step.name for step in pipeline.steps]
    assert names.index("git.require_synced_main") < names.index("release.require_version_available")
    assert names.index("release.require_version_available") < names.index("python.prepare_release")
    assert names.index("python.prepare_release") < names.index("git.release_commit")
    assert names.index("git.release_commit") < names.index("git.release_tag_push")
    assert names.index("git.release_tag_push") < names.index("github.wait_release")


def test_release_pipeline_stages_only_explicit_release_files():
    pipeline = _release_pipeline()

    commit_options = pipeline.steps[RELEASE_STEP_ORDER.index("git.release_commit")].options
    assert sorted(commit_options["files"]) == sorted(RELEASE_FILES)

    prepare_options = pipeline.steps[RELEASE_STEP_ORDER.index("python.prepare_release")].options
    assert prepare_options["pyproject"] == "pyproject.toml"
    assert prepare_options["version_file"] == "cdt/__init__.py"
    assert prepare_options["changelog"] == "CHANGELOG.md"
    assert sorted(prepare_options["tag_reference_files"]) == sorted(["README.md", "docs/getting-started.md"])
    assert prepare_options["tag_reference_regex"] == TAG_REFERENCE_REGEX


def test_release_pipeline_waits_for_terminal_release_confirmation():
    pipeline = _release_pipeline()

    wait_options = pipeline.steps[RELEASE_STEP_ORDER.index("github.wait_release")].options
    assert wait_options["repository"] == "Sergionius/cdt"
    assert wait_options["workflow"] == "release.yml"
    assert wait_options["package"] == "cdt-release"
    assert wait_options["version"] == "${inputs.version}"
