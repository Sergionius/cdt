"""Tests for per-user experimental settings, including experimental.run-retention."""

import json

import pytest
from typer.testing import CliRunner

from cdt.cli import app
from cdt.user_settings import (
    EXPERIMENTAL_FLAGS,
    ORCA_STATUS,
    RUN_RETENTION,
    experimental_enabled,
    orca_status_enabled,
    run_retention_enabled,
    set_experimental_flag,
    set_run_retention,
    settings_path,
)

runner = CliRunner()


def test_experimental_flags_cover_orca_status_and_run_retention():
    assert EXPERIMENTAL_FLAGS == {ORCA_STATUS: "orca_status", RUN_RETENTION: "run_retention"}


def test_both_flags_default_to_disabled_without_settings_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert not orca_status_enabled()
    assert not run_retention_enabled()


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not-json",
        "[1, 2]",
        '{"experimental": "yes"}',
        '{"experimental": []}',
        '{"experimental": {"other": true}}',
        '{"unrelated": true}',
    ],
)
def test_missing_or_invalid_settings_never_enable_any_flag(tmp_path, monkeypatch, text):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

    assert not orca_status_enabled()
    assert not run_retention_enabled()


def test_run_retention_cli_round_trip_states_scope_and_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    enabled = runner.invoke(app, ["settings", "enable", RUN_RETENTION])

    assert enabled.exit_code == 0
    assert run_retention_enabled() is True
    assert "global" in enabled.output
    assert "current project" in enabled.output
    assert "30 days" in enabled.output
    assert "10 newest" in enabled.output

    shown = runner.invoke(app, ["settings", "show"])

    assert shown.exit_code == 0
    assert f"{ORCA_STATUS}: disabled" in shown.output
    assert f"{RUN_RETENTION}: enabled" in shown.output
    assert "global" in shown.output

    disabled = runner.invoke(app, ["settings", "disable", RUN_RETENTION])

    assert disabled.exit_code == 0
    assert run_retention_enabled() is False
    shown = runner.invoke(app, ["settings", "show"])
    assert f"{RUN_RETENTION}: disabled" in shown.output
    assert "30 days" not in shown.output


def test_orca_status_cli_behavior_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert runner.invoke(app, ["settings", "enable", ORCA_STATUS]).exit_code == 0
    assert orca_status_enabled() is True
    # The flags are independent: enabling one never enables the other.
    assert run_retention_enabled() is False
    shown = runner.invoke(app, ["settings", "show"])
    assert f"{ORCA_STATUS}: enabled" in shown.output
    assert f"{RUN_RETENTION}: disabled" in shown.output
    assert runner.invoke(app, ["settings", "disable", ORCA_STATUS]).exit_code == 0
    assert orca_status_enabled() is False


def test_unknown_setting_lists_every_available_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    result = runner.invoke(app, ["settings", "enable", "unknown"])
    assert result.exit_code != 0
    assert ORCA_STATUS in result.output
    assert RUN_RETENTION in result.output


def test_flag_changes_preserve_neighbouring_settings_and_leave_no_temp_files(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = settings_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"other": {"a": 1}, "experimental": {"legacy": True, "orca_status": True}}),
        encoding="utf-8",
    )

    assert runner.invoke(app, ["settings", "enable", RUN_RETENTION]).exit_code == 0

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["other"] == {"a": 1}
    assert data["experimental"] == {"legacy": True, "orca_status": True, "run_retention": True}
    assert not list(path.parent.glob(".settings.json.*.tmp"))


def test_invalid_experimental_section_fails_closed_for_reads_and_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = settings_path()
    path.parent.mkdir(parents=True)
    path.write_text('{"experimental": ["nope"]}', encoding="utf-8")

    assert not run_retention_enabled()
    result = runner.invoke(app, ["settings", "enable", RUN_RETENTION])

    assert result.exit_code != 0
    assert "Cannot save CDT settings" in result.output
    assert json.loads(path.read_text(encoding="utf-8")) == {"experimental": ["nope"]}


def test_set_run_retention_helper_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert set_run_retention(True) == settings_path()
    assert run_retention_enabled() is True
    assert set_run_retention(False) == settings_path()
    assert run_retention_enabled() is False


def test_unknown_flag_names_are_rejected_by_helpers():
    with pytest.raises(ValueError, match="Unknown experimental setting"):
        experimental_enabled("experimental.nope")
    with pytest.raises(ValueError, match="Unknown experimental setting"):
        set_experimental_flag("experimental.nope", True)
