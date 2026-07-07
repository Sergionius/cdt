# Clean 0.2.0 UX/runtime cleanup

## Summary

Довести YAML-only 0.2.0 до более чистого состояния: убрать misleading notify message, удалить старые value aliases, убрать side effect из interpolation, улучшить ошибку отсутствующего `cdt.yaml`, чистить успешные temp logs и задокументировать parallel context limitations.

## Key Changes

### 1. `NotifySuccessStep`

- Убрать параметр `command_name`.
- Добавить параметр `message: str | None = None`.
- Default echo:

```text
✅ Pipeline completed
```

- Custom echo:

```text
✅ {message}
```

- `include_ids` оставить без изменений.
- Убрать hardcoded текст `iOS TestFlight flow`.

### 2. `IncrementFlutterBuildNumberStep`

- Удалить старые aliases:
  - `flutter_version_old`;
  - `flutter_version`.
- Оставить только v1 keys:
  - `flutter.version.old`;
  - `flutter.version`;
  - `flutter.build_number`.

### 3. `_resolve_expression`

- `${flutter.version}` должен читать текущую версию из `pubspec.yaml` без записи в `ctx.values`.
- Убрать mutation `ctx.values["flutter_version"] = version`.

### 4. `load_pipeline_config`

- Улучшить ошибку отсутствующего `cdt.yaml`.
- Новый текст должен содержать hint:

```text
See examples/cdt.yaml.
```

### 5. `CommandRunner._run`

- При успешной команде удалять temporary log file.
- При ошибке оставлять temporary log file.
- При ошибке показывать путь к log file вместе с tail.
- `spawn` logs оставить без изменений, потому что процесс асинхронный.

### 6. `docs/pipelines.md`

- Задокументировать parallel context limitations:
  - `ctx.artifacts` / artifact registration is thread-safe;
  - запись в `ctx.values` из parallel branches не гарантируется thread-safe;
  - parallel branches не должны зависеть друг от друга через `ctx.values`.

## Test Plan

Добавить/обновить тесты для:

- generic/custom `notify.success`;
- отсутствия старых `flutter_version_*` aliases;
- `${flutter.version}` без mutation `ctx.values`;
- improved `cdt.yaml` missing error;
- successful `_run` deletes temp log;
- failed `_run` keeps temp log and reports path.

Run:

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m compileall cdt tests
.venv/bin/python -m pytest -q
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
```

## Save Path

`plans/2026-07-04-clean-020-runtime-ux.md`

## Out of Scope

- `notify.failure`;
- full thread-safety for `ctx.values`;
- redesign parallel context model.
