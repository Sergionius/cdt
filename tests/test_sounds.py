import subprocess

from cdt import sounds


def test_play_named_sound_no_mode_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(sounds.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError))

    sounds._play_named_sound(
        {},
        tmp_path,
        mode_key="SUCCESS_SOUND",
        file_key="SUCCESS_SOUND_FILE",
        default_file="default",
        label="success",
    )


def test_play_named_sound_rejects_unknown_mode(monkeypatch, tmp_path, capsys):
    sounds._play_named_sound(
        {"SUCCESS_SOUND": "linux"},
        tmp_path,
        mode_key="SUCCESS_SOUND",
        file_key="SUCCESS_SOUND_FILE",
        default_file="default",
        label="success",
    )

    assert "Unknown SUCCESS_SOUND mode: linux" in capsys.readouterr().err


def test_play_named_sound_uses_default_file_and_clamps_volume(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        sounds.subprocess,
        "run",
        lambda command, cwd, capture_output, text: calls.append((command, cwd, capture_output, text))
        or subprocess.CompletedProcess(command, 0),
    )

    sounds._play_named_sound(
        {"SUCCESS_SOUND": "macos", "SOUND_VOLUME": "2"},
        tmp_path,
        mode_key="SUCCESS_SOUND",
        file_key="SUCCESS_SOUND_FILE",
        default_file="default.aiff",
        label="success",
    )

    assert calls == [(["afplay", "-v", "1.0", "default.aiff"], tmp_path, True, True)]


def test_play_named_sound_uses_relative_custom_file_and_invalid_volume_default(monkeypatch, tmp_path, capsys):
    custom = tmp_path / "sound.aiff"
    custom.write_text("fake", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        sounds.subprocess,
        "run",
        lambda command, cwd, capture_output, text: calls.append(command) or subprocess.CompletedProcess(command, 0),
    )

    sounds._play_named_sound(
        {"SUCCESS_SOUND": "macos", "SUCCESS_SOUND_FILE": "sound.aiff", "SOUND_VOLUME": "loud"},
        tmp_path,
        mode_key="SUCCESS_SOUND",
        file_key="SUCCESS_SOUND_FILE",
        default_file="default.aiff",
        label="success",
    )

    assert calls == [["afplay", "-v", "0.3", str(custom)]]
    assert "Invalid SOUND_VOLUME value: loud" in capsys.readouterr().err


def test_play_named_sound_reports_missing_custom_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sounds.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError))

    sounds._play_named_sound(
        {"SUCCESS_SOUND": "macos", "SUCCESS_SOUND_FILE": "missing.aiff"},
        tmp_path,
        mode_key="SUCCESS_SOUND",
        file_key="SUCCESS_SOUND_FILE",
        default_file="default.aiff",
        label="success",
    )

    assert "SUCCESS_SOUND_FILE not found" in capsys.readouterr().err


def test_play_named_sound_reports_missing_afplay(monkeypatch, tmp_path, capsys):
    def fake_run(command, cwd, capture_output, text):
        raise FileNotFoundError

    monkeypatch.setattr(sounds.subprocess, "run", fake_run)

    sounds._play_named_sound(
        {"SUCCESS_SOUND": "macos"},
        tmp_path,
        mode_key="SUCCESS_SOUND",
        file_key="SUCCESS_SOUND_FILE",
        default_file="default.aiff",
        label="success",
    )

    assert "afplay not found" in capsys.readouterr().err


def test_play_named_sound_reports_failed_play(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        sounds.subprocess,
        "run",
        lambda command, cwd, capture_output, text: subprocess.CompletedProcess(command, 1, stderr="boom"),
    )

    sounds._play_named_sound(
        {"SUCCESS_SOUND": "macos"},
        tmp_path,
        mode_key="SUCCESS_SOUND",
        file_key="SUCCESS_SOUND_FILE",
        default_file="default.aiff",
        label="success",
    )

    assert "Failed to play success sound: boom" in capsys.readouterr().err


def test_play_success_and_fail_sound_delegate(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sounds, "_play_named_sound", lambda *args, **kwargs: calls.append((args, kwargs)))

    sounds._play_success_sound({"SUCCESS_SOUND": "macos"}, tmp_path)
    sounds._play_fail_sound({"FAIL_SOUND": "macos"}, tmp_path)

    assert calls[0][1]["mode_key"] == "SUCCESS_SOUND"
    assert calls[0][1]["label"] == "success"
    assert calls[1][1]["mode_key"] == "FAIL_SOUND"
    assert calls[1][1]["label"] == "fail"
