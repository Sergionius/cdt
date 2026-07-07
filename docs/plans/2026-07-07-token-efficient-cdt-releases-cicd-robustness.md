# Token-efficient CDT releases + CI/CD robustness

## Summary

Сделать одним пакетом работ две вещи:

1. снизить расход context-window при долгих `cdt run` через agent-friendly release protocol;
2. довести CI/CD-план из `docs/plans/2026-07-07-cicd-robustness-rm-rf-dist-pytest-q-retry.md`.

Ключевое решение: вместо repo-local `scripts/cdt-agent-release-*` делать переносимый CLI внутри CDT: `cdt agent-release start/status/stop`. Так команды будут доступны в любом проекте, где установлен CDT, а не только в репозитории CDT.

## Key Changes

### 1. Agent release helper в CDT CLI

Добавить Typer-группу:

```bash
cdt agent-release start <pipeline> [--id ID ...]
cdt agent-release status <pipeline> [--wait] [--timeout 40m] [--json]
cdt agent-release stop <pipeline> [--timeout 30s]
```

Поведение:

- `start` выполняет preflight только технически минимально: создаёт `.cdt/`, запускает `cdt run <pipeline>` в фоне, пишет:
  - `.cdt/agent-release-<pipeline>.log`
  - `.cdt/agent-release-<pipeline>.pid`
  - `.cdt/agent-release-<pipeline>.meta.json`
  - `.cdt/agent-release-<pipeline>.exit` после завершения
- `status` не читает лог в stdout, а возвращает компактный статус:
  - `status: running|success|failed|stale|unknown`
  - `pipeline`
  - `pid`
  - `exit_code`
  - `log`
  - `last_log_update`
  - `status_file`, если есть
- `status --wait` ждёт внутри процесса и печатает только финальный компактный результат.
- `stop` сначала делает graceful terminate, потом kill после timeout.

### 2. Machine-readable status для `cdt run`

Добавить опцию:

```bash
cdt run <pipeline> --status-file .cdt/agent-release-<pipeline>.status.json
```

Статус обновляется без чтения лога:

- pipeline started/finished/failed;
- current step;
- completed steps;
- failed step/error;
- artifacts из `PipelineContext.artifacts`;
- version fields, если доступны;
- timestamps.

Для parallel steps статус должен быть безопасным к concurrent update: через lock или атомарную запись temp-file + rename.

### 3. Token-efficient release skill

Обновить `skills/cdt-release/SKILL.md` и `.agents/rules/cdt-release.md`:

- использовать `cdt agent-release start/status --wait`;
- не делать `tail` при нормальном ходе;
- не писать промежуточные “всё ещё ждём”;
- максимум: стартовое сообщение и финальный YAML/JSON summary;
- `tail` разрешён только при failure/debug, коротко и явно;
- production confirmation правила оставить без изменений.

### 4. CI/CD robustness из существующего плана

В `.github/workflows/ci.yml`:

- заменить build step на:

```yaml
run: |
  rm -rf dist
  python -m build
```

Проверить консистентность:

- все workflow pytest-команды используют `-q`;
- `pr.yml` и `release.yml` уже имеют `rm -rf dist`;
- `release.yml` уже имеет retry `for attempt in 1 2 3` + `sleep 10` для `github-tag-smoke`.

Добавить workflow regression tests, чтобы эти правила не сломались снова.

## Test Plan

Добавить/обновить тесты:

- `tests/test_cli.py`
  - help показывает `agent-release`;
  - `cdt agent-release start --help/status --help/stop --help`.
- Новый `tests/test_agent_release.py`
  - `start` создаёт `.cdt` файлы и не стримит лог;
  - `status` возвращает компактный YAML/JSON;
  - `status --wait` ждёт завершение и печатает только summary;
  - `stop` корректно обрабатывает missing/stale pid.
- `tests/test_pipeline_status_file.py`
  - `cdt run demo --status-file ...` пишет started/success;
  - при ошибке пишет failed step/error;
  - artifacts попадают в status file.
- Новый `tests/test_workflows.py`
  - `ci.yml` build содержит `rm -rf dist` перед `python -m build`;
  - все workflow pytest-команды содержат `-q`;
  - `release.yml` tag smoke содержит 3 attempts и `sleep 10`.

Финальная проверка:

```bash
python -m pytest -q
python -m ruff check .
rm -rf dist && python -m build
python -m twine check dist/*
```

## Assumptions

- Основной переносимый интерфейс будет `cdt agent-release ...`, а не standalone scripts в `scripts/`.
- `cdt agent-release start` не заменяет safety preflight из skill: агент всё равно обязан читать `cdt.yaml`, делать `cdt pipeline list` и `cdt pipeline inspect <pipeline>` перед реальным запуском.
- Финальный ответ агента строится из status/meta/status-file, а лог читается только при ошибке.
