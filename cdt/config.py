import os
import time
from pathlib import Path

from dotenv import dotenv_values

UI_MODE = "pretty"
DEBUG_LOG_PATH: Path | None = None


def _load_project_env(cwd: Path) -> dict[str, str]:
    env_file = cwd / ".env"
    file_values = dotenv_values(env_file) if env_file.exists() else {}
    env: dict[str, str] = {k: str(v) for k, v in file_values.items() if v is not None}
    env.update({k: v for k, v in os.environ.items() if isinstance(v, str)})
    return env


def _set_ui_mode(env: dict[str, str]) -> None:
    global UI_MODE, DEBUG_LOG_PATH
    mode = env.get("CDT_UI", "pretty").strip().lower() or "pretty"
    UI_MODE = mode if mode in {"pretty", "verbose"} else "pretty"

    debug_dir = Path(env.get("CDT_DEBUG_DIR", ".cdt")).expanduser()
    if not debug_dir.is_absolute():
        debug_dir = Path.cwd() / debug_dir
    debug_dir.mkdir(parents=True, exist_ok=True)
    DEBUG_LOG_PATH = debug_dir / "last_run.log"


def _debug_log(message: str) -> None:
    if not DEBUG_LOG_PATH:
        return
    try:
        with DEBUG_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception:
        pass
