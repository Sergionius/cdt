# Agent release review fixes

## Summary

Закрыть замечания по `cdt agent-release` перед merge: сделать worker устойчивым к ошибкам старта, улучшить timeout-семантику, выдавать валидный YAML, расширить CI smoke и обновить changelog.

## Key Changes

1. `agent_release_worker.py`
   - Обернуть `subprocess.Popen(...).wait()` в `try/except`.
   - При exception писать ошибку/traceback в log.
   - Гарантированно писать `exit_file = 1`.
2. `wait_for_release`
   - При timeout не заменять фактический `status`.
   - Возвращать `status: running` и `wait_status: timeout`.
3. `format_yamlish`
   - Перевести на `yaml.safe_dump(..., allow_unicode=True, sort_keys=False)`.
4. `ci.yml`
   - Добавить smoke:
     - `python -m cdt agent-release --help`
     - `python -m cdt agent-release start --help`
     - `python -m cdt agent-release status --help`
     - `python -m cdt agent-release stop --help`
5. `CHANGELOG.md`
   - Обновить `Unreleased` про `agent-release`, `--status-file`, CI/CD robustness.
6. `meta.json`
   - Оставить логическую `command`.
   - Добавить фактическую `worker_command`.

## Deferred

Не делать safer PID identity через `/proc`/create_time в этом PR: это не portable на macOS, а добавлять `psutil` не хочется. Текущего `start_new_session=True` + `killpg` достаточно для v1.

## Test Plan

- Добавить тест: worker пишет `exit_file=1`, если `Popen` бросает exception.
- Добавить тест: timeout возвращает `status: running`, `wait_status: timeout`.
- Добавить тест: CLI YAML output парсится через `yaml.safe_load`.
- Добавить тест: `ci.yml` smoke содержит `agent-release` help-команды.
- Проверить:
  - `.venv/bin/python -m ruff check .`
  - `.venv/bin/python -m pytest -q`
  - `rm -rf dist && .venv/bin/python -m build`
  - `.venv/bin/python -m twine check dist/*`
