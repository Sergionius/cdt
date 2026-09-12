<!-- ralphex-base: 66a2377cea0e612d9013045db0d35d0ab4a2973b -->

# Перевод собственного релиза CDT на `cdt.yaml`

## Goal

Сделать CDT единственным поддерживаемым способом подготовки и публикации собственных релизов CDT. Релиз должен запускаться с явно переданной версией, требовать точного production-подтверждения, безопасно изменять release-файлы, создавать и атомарно отправлять release commit/tag, ждать GitHub Actions и подтверждать наличие той же версии в GitHub Releases и PyPI.

Целевая команда:

```bash
cdt run release --input version=0.5.2 --confirm release
```

Для первого dogfood-запуска после реализации используется бинарник из текущего checkout:

```bash
.venv/bin/cdt run release --input version=0.5.2 --confirm release
```

## Context

Сейчас собственный релиз CDT готовится через `scripts/release.py`. Скрипт обновляет версии в `pyproject.toml` и `cdt/__init__.py`, переносит changelog, обновляет ссылки на GitHub tag, запускает ruff/pytest/build, создаёт commit и annotated tag, затем при `--push` отправляет их в origin.

Публикация выполняется `.github/workflows/release.yml`: один job повторно запускает проверки и сборку, публикует `cdt-release` в PyPI через Trusted Publishing/OIDC, создаёт GitHub Release и прикладывает wheel/sdist/checksums. Отдельный job проверяет установку из GitHub tag.

В корне репозитория нет `cdt.yaml`. Текущая pipeline-модель поддерживает `risk`, steps, runtime interpolation из env/ids/values, direct и detached execution, run manifests/status, resume и generated JSON Schema. Декларативных pipeline inputs пока нет. Detached execution передаёт только pipeline, ids и confirmation.

Built-in шаги регистрируются в `cdt/pipeline/builtins.py`, их схемы генерируются из сигнатур через `cdt/schema.py`. Команды выполняются через `CommandRunner`. GitHub-интеграция нового release waiter должна использовать установленный `gh` CLI; PyPI проверяется через публичный JSON API. Новых Python-зависимостей для HTTP-клиента не требуется.

Автоматическое исправление упавшего кода остаётся поведением агента, описанным в skill/rules, а не бесконтрольным циклом внутри CDT. После исправления агент работает через отдельную `fix/*` ветку и PR, самостоятельно мёржит зелёный PR, синхронизирует `main` и получает новое точное production-подтверждение.

## Scope

- Декларативные несекретные pipeline inputs с CLI-передачей, interpolation, validation, run persistence и resume consistency.
- Built-in шаги для стандартного Python release toolchain: ruff, pytest, build и twine.
- Release preflight для версии, git state, GitHub Releases и PyPI.
- Безопасная подготовка version/changelog/tag references с rollback до release commit.
- Точный staging release-файлов, annotated tag и atomic git push.
- Ожидание GitHub Actions через `gh`, GitHub Release и PyPI package.
- Корневой production pipeline для собственного релиза CDT.
- Разделение GitHub Actions release workflow на повторно запускаемые jobs.
- Удаление `scripts/release.py` и перевод документации/agent guidance на CDT pipeline.

## Out of Scope

- Poetry, Hatch и uv как release backends.
- Произвольный shell executor.
- Передача секретов через pipeline inputs.
- Автоматический выбор следующей версии.
- Перемещение или force-push существующего git tag.
- Повторная публикация уже существующей версии в PyPI.
- Реализация GitHub PR-бота внутри CDT; repair-loop выполняется агентом по repository skill/rules.
- Выполнение реального production-релиза в рамках реализации этого плана.

## Implementation Steps

### Task 1: Добавить декларативные pipeline inputs во все режимы запуска

**Files:**
- Modify: `cdt/pipeline/config.py`
- Modify: `cdt/pipeline/context.py`
- Modify: `cdt/pipeline/runner.py`
- Modify: `cdt/pipeline/planning.py`
- Modify: `cdt/pipeline/validation.py`
- Modify: `cdt/cli.py`
- Modify: `cdt/agent_release.py`
- Modify: `cdt/agent_release_worker.py`
- Modify: `cdt/runs.py`
- Modify: `cdt/schema.py`
- Modify: `cdt/cdt.schema.json`
- Modify: `tests/test_pipeline_config.py`
- Modify: `tests/test_pipeline_context.py`
- Modify: `tests/test_pipeline_plan.py`
- Modify: `tests/test_pipeline_status_file.py`
- Modify: `tests/test_pipeline_resume.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_agent_release.py`
- Modify: `tests/test_agent_first.py`

- [x] Расширить `PipelineSpec` декларациями `inputs`, где каждый input поддерживает `required: bool` и необязательный regex `pattern`; отклонять неизвестные поля, некорректные имена, типы и regex при загрузке/validation.
- [x] Добавить повторяемый `--input KEY=VALUE` для `cdt run`, включая `--dry-run`, и `cdt agent-release start`; отклонять записи без `=`, пустые ключи, дубликаты, неизвестные inputs, отсутствующие required inputs и значения, не прошедшие `fullmatch`.
- [x] Добавить inputs в `PipelineContext` и interpolation `${inputs.<name>}` с понятной ошибкой для отсутствующего значения.
- [x] Передавать inputs через direct runner, detached launcher и worker без потери порядка/значений; включать их в воспроизводимую command line run manifest.
- [x] Сохранять несекретные inputs в manifest/status и compact agent-release status; применять существующую defense-in-depth redaction перед записью.
- [x] При resume требовать совпадения входов с исходным run и не позволять продолжить release с другим номером версии.
- [x] Показывать объявления inputs в inspect/plan JSON и human output, не раскрывая несуществующих runtime значений.
- [x] Расширить generated и bundled JSON Schema полем `pipelines.<name>.inputs`, сохранив обратную совместимость существующих `cdt.yaml` версии 1.
- [x] Добавить unit/CLI/detached/resume тесты для корректных inputs, всех validation errors, interpolation, persistence, worker propagation и production-команды с `--input`.

### Task 2: Реализовать безопасные Python release и git built-in шаги

**Files:**
- Create: `cdt/steps/python.py`
- Create: `cdt/steps/release.py`
- Modify: `cdt/steps/git.py`
- Modify: `cdt/pipeline/context.py`
- Modify: `cdt/pipeline/runner.py`
- Modify: `cdt/pipeline/builtins.py`
- Modify: `cdt/schema.py`
- Modify: `cdt/cdt.schema.json`
- Create: `tests/test_steps_python.py`
- Create: `tests/test_steps_release.py`
- Modify: `tests/test_runner.py`
- Modify: `tests/test_pipeline_error_ux.py`
- Modify: `tests/test_pipeline_resume.py`
- Modify: `tests/test_agent_first.py`

- [x] Добавить `git.require_synced_main`: выполнить fetch origin/main и tags, потребовать clean tracked working tree, текущую ветку `main`, настроенный origin и точное равенство локального `HEAD` и `origin/main`.
- [x] Добавить `release.require_version_available` с обязательной explicit semver: версия должна быть строго новее package version и отсутствовать в changelog, локальных/remote tags, GitHub Releases и PyPI; проверки GitHub выполнять через `gh`, PyPI — через публичный JSON API с bounded retries/backoff.
- [x] Добавить `python.ruff_check` и `python.pytest`, запускающие утверждённые команды `ruff check .` и `pytest -q` через `CommandRunner` с сохранением исходной команды/exit code в ошибках.
- [x] Добавить `python.prepare_release` со структурированными путями к `pyproject.toml`, Python version file, changelog и списку tag-reference files; валидировать ровно одно ожидаемое version occurrence в каждом файле до записи.
- [x] Реализовать changelog-переход: потребовать непустой реальный `Unreleased`, создать `## vX.Y.Z - YYYY-MM-DD`, перенести записи без `TODO` и восстановить `Unreleased` с `- Nothing yet.`.
- [x] Сохранять точные snapshots изменяемых release-файлов в run context/run directory и регистрировать rollback до release commit; при любой последующей ошибке до commit восстанавливать только эти файлы и отражать `rolled_back` в status.
- [x] Добавить `python.build_distribution`: удалить только настроенный `dist/`, выполнить текущим Python `-m build`, затем `-m twine check` для полученных wheel/sdist, проверить наличие обоих типов файлов и зарегистрировать их как artifacts.
- [x] Добавить `git.release_commit`: stage только явно настроенные release-файлы, убедиться в отсутствии других staged изменений, создать commit `Release vX.Y.Z`, проверить его содержимое и после успеха закрыть rollback boundary.
- [x] Добавить resumable/idempotent `git.release_tag_push`: создать annotated tag на release commit, отклонить конфликтующий локальный/remote tag и отправить `main` вместе с tag через `git push --atomic`.
- [x] Зарегистрировать шаги с точными metadata category/risk/tools/artifact requirements и обновить generated/bundled schema.
- [x] Покрыть команды, semver conflicts, changelog edge cases, exact staging, rollback, artifact discovery, atomic push, remote conflicts, idempotent resume и failure diagnostics тестами без реального git push или сетевых запросов.

### Task 3: Добавить ожидание GitHub Actions, GitHub Release и PyPI

**Files:**
- Create: `cdt/steps/github.py`
- Create: `cdt/services/pypi.py`
- Modify: `cdt/pipeline/builtins.py`
- Modify: `cdt/schema.py`
- Modify: `cdt/cdt.schema.json`
- Create: `tests/test_steps_github.py`
- Create: `tests/test_services_pypi.py`
- Modify: `tests/test_pipeline_status_file.py`
- Modify: `tests/test_agent_first.py`

- [x] Реализовать `github.wait_release` с параметрами repository, workflow, package, version, timeout и poll interval; объявить `gh` обязательным external tool.
- [x] Через machine-readable `gh` output найти workflow run для точного release tag/commit, дождаться terminal conclusion и завершиться ошибкой с run URL/job summary при неуспехе.
- [x] После зелёного workflow проверить через `gh release view`, что release не draft/prerelease и содержит wheel, sdist и `SHA256SUMS`.
- [x] Проверить через PyPI JSON API наличие точной версии и wheel/sdist; применять ограниченные retries/backoff для задержки индексации и временных HTTP/network failures.
- [x] Зарегистрировать подтверждённые GitHub Release/PyPI URLs и package artifacts/results в context/status, чтобы итоговый agent-release summary не требовал чтения полного лога.
- [x] Не перезапускать workflow автоматически внутри built-in шага: возвращать структурированную классификацию timeout, transient GitHub/PyPI failure и terminal workflow failure для решения агентом.
- [x] Обновить metadata/schema и покрыть delayed workflow discovery, timeout, failed conclusion, malformed `gh` JSON, missing assets, delayed PyPI propagation и success path тестами.

### Task 4: Сделать GitHub Actions release workflow безопасно перезапускаемым

**Files:**
- Modify: `.github/workflows/release.yml`
- Modify: `tests/test_agent_first.py`

- [x] Разделить workflow на dependency-ordered jobs: validate/build, PyPI publish, GitHub Release и tag smoke.
- [x] В validate/build выполнить checkout точного tag, install dev dependencies, ruff, pytest, clean build и twine check, затем загрузить wheel/sdist как Actions artifact.
- [x] В PyPI job скачать проверенные artifacts и оставить Trusted Publishing/OIDC единственным способом публикации.
- [x] В GitHub Release job, зависящем от успешной PyPI publication, скачать те же artifacts, сгенерировать checksums и создать release с wheel/sdist/checksum.
- [x] В smoke job, зависящем от GitHub Release, сохранить установку из точного GitHub tag и проверку `cdt --version` на ожидаемую tag version.
- [x] Не объединять PyPI upload с последующими потенциально падающими операциями, чтобы rerun failed jobs не пытался повторно публиковать уже принятую версию.
- [x] Расширить структурные workflow-тесты зависимостями jobs, permissions, artifact handoff, trusted publishing и smoke ordering.

### Task 5: Перевести репозиторий и agent workflow на собственный CDT pipeline

**Files:**
- Create: `cdt.yaml`
- Delete: `scripts/release.py`
- Modify: `tests/test_release_script.py`
- Modify: `README.md`
- Modify: `docs/getting-started.md`
- Modify: `docs/pipelines.md`
- Modify: `docs/runs.md`
- Modify: `docs/ai-agents.md`
- Modify: `docs/skills.md`
- Modify: `skills/cdt-release/SKILL.md`
- Modify: `.agents/rules/cdt-release.md`
- Modify: `AGENTS.md`
- Modify: `CHANGELOG.md`

- [ ] Добавить корневой `cdt.yaml` с production pipeline `release`, required semver input `version` и последовательностью sync/version preflight, ruff, pytest, prepare, build, commit, atomic tag push и release wait.
- [ ] Настроить release-файлы явно: `pyproject.toml`, `cdt/__init__.py`, `CHANGELOG.md`, `README.md`, `docs/getting-started.md`; исключить широкое `git add .`.
- [ ] Удалить legacy helper и заменить его тесты проверками, что repository release pipeline существует, валиден, production-marked, использует explicit input и содержит безопасный порядок шагов.
- [ ] Переписать release documentation на `cdt pipeline list/inspect/preflight`, dry-run и точную production-команду; отдельно документировать bootstrap через `.venv/bin/cdt`.
- [ ] Документировать обязательные инструменты `git`, `gh`, `ruff`, `pytest`, `build`, `twine`, а также публичную PyPI-проверку и GitHub authentication через активную сессию `gh auth`.
- [ ] Обновить skill/rules: агент определяет предлагаемую следующую версию, но передаёт её явно; после preflight получает точное confirmation; ждёт terminal release result и не считает один push успешным релизом.
- [ ] Описать repair-loop: при кодовой ошибке до публикации rollback, отдельная `fix/<short-name>` ветка от свежего main, минимальное исправление, PR, ожидание CI, автоматический merge/delete branch, возврат к синхронному main и новое production-подтверждение.
- [ ] Ограничить одинаковые автоматические попытки исправления тремя; после этого агент сообщает blocked. Запретить движение существующего tag и повторное использование опубликованной PyPI-версии.
- [ ] Обновить changelog записью о declarative inputs, Python release built-ins и dogfooding pipeline.

## Validation

```bash
pytest tests/test_pipeline_config.py tests/test_pipeline_context.py tests/test_pipeline_plan.py
pytest tests/test_cli.py tests/test_agent_release.py tests/test_pipeline_status_file.py tests/test_pipeline_resume.py
pytest tests/test_steps_python.py tests/test_steps_release.py tests/test_steps_github.py tests/test_services_pypi.py
pytest tests/test_release_script.py tests/test_agent_first.py tests/test_pipeline_error_ux.py tests/test_runner.py
ruff check .
pytest
pytest --cov=cdt --cov-report=term
rm -rf dist
python -m build
.venv/bin/cdt pipeline validate --strict
.venv/bin/cdt pipeline inspect release
.venv/bin/cdt run release --input version=0.5.2 --dry-run
```

Последние команды используют заведомо ещё не опубликованную тестовую semver, выбранную во время реализации; dry-run не должен изменять git, release-файлы, run records, GitHub или PyPI.

## Acceptance Criteria

- В корне репозитория находится валидный production pipeline `release`; `scripts/release.py` отсутствует.
- Версия передаётся только явно через `--input version=...`, валидируется и одинаково работает в direct/dry-run/detached/resume режимах.
- Release не начинается при dirty/non-main/diverged/ahead/behind repository state либо при уже занятой версии.
- До release commit любые ошибки восстанавливают изменённые release-файлы; посторонние файлы не stage и не восстанавливаются.
- Commit содержит только разрешённые release-файлы, annotated tag указывает на него, branch/tag отправляются atomic push без перемещения существующих тегов.
- Pipeline завершается success только после зелёного workflow, опубликованного GitHub Release с тремя типами assets и доступной wheel/sdist версии в PyPI.
- GitHub Actions jobs допускают безопасный rerun failed jobs без повторной отправки уже опубликованного PyPI-файла.
- Agent guidance требует отдельный fix PR, зелёный CI, автоматический merge и новое точное production-подтверждение после изменения кода.
- Текущие pipelines без `inputs` сохраняют прежнее поведение.
