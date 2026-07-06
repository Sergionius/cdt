---
# Добавить команду `cdt self-update` для обновления до последнего релиза с GitHub

## Overview
Добавить в CDT новую top-level команду `cdt self-update`, которая определяет последний release-тег в репозитории `Sergionius/cdt` через GitHub API и переустанавливает пакет из GitHub. Команда ориентирована на рекомендуемый способ установки через `pipx`, но корректно сообщает, если CDT установлен иным способом.

## Context
- Файлы:
  - `cdt/cli.py` — точка входа CLI, регистрация команд
  - `cdt/__init__.py` — текущая версия `__version__`
  - `cdt/ui.py` / `cdt/config.py` — существующие helper'ы для вывода
  - `pyproject.toml` — метаданные проекта, `project.urls.Repository`
  - `README.md` — список команд и инструкции по установке
  - `CHANGELOG.md` — история изменений
- Связанные паттерны:
  - Команды реализуются через `typer` (`@app.command(...)`)
  - JSON/машиночитаемый вывод для служебных команд — через `json.dumps(..., sort_keys=True)`
  - Тесты CLI используют `typer.testing.CliRunner` (`tests/test_cli.py`)
  - Работа с внешними API — лучше через stdlib (`urllib.request`), чтобы не добавлять зависимость
- Внешние зависимости:
  - GitHub REST API (`https://api.github.com/repos/{owner}/{repo}/releases/latest`)
  - `pipx` как целевой менеджер установки/обновления

## Development Approach
- Testing approach: Regular (код, затем тесты)
- Выполнять каждую задачу полностью перед переходом к следующей
- **CRITICAL: every task MUST include new/updated tests**
- **CRITICAL: all tests must pass before starting next task**
- Избегать добавления новых сторонних зависимостей
- Не поддерживать гипотетические сценарии (editable-установка обновляется вручную, не через self-update)

## Implementation Steps

### Task 1: Создать модуль `cdt/self_update.py`

**Files:**
- Create: `cdt/self_update.py`

- [x] Реализовать `_latest_release_tag(owner: str, repo: str) -> str` через `urllib.request` + `json.loads`
- [x] Обработать ошибки сети/API и отсутствие release-тега с понятными сообщениями (`typer.BadParameter` или кастомные исключения)
- [x] Реализовать `_detect_install_method() -> str | None`, определяющий `pipx`/`pip` по `sys.executable` / `pipx` metadata / `pip list`
- [x] Реализовать `_update_command(tag: str, method: str) -> list[str]`, возвращающий аргументы для `pipx install --force git+https://github.com/{owner}/{repo}.git@{tag}` (или аналог для `pip`)
- [x] Реализовать `run_self_update(*, repo_url: str, dry_run: bool = False) -> None`, который выводит текущую и новую версию, формирует команду и выполняет через `subprocess.run(...)` (или только печатает при `dry_run=True`)
- [x] Написать unit-тесты для парсинга ответа GitHub API, определения метода установки и формирования команды обновления
- [x] Запустить `pytest tests/test_self_update.py` — должен пройти

### Task 2: Добавить команду `cdt self-update` в CLI

**Files:**
- Modify: `cdt/cli.py`

- [x] Добавить `@app.command(name="self-update") def self_update(...)` с опциональным флагом `--dry-run`
- [x] Использовать `__version__` как текущую версию
- [x] Использовать URL репозитория из `pyproject.toml` (`project.urls.Repository`) либо константу `Sergionius/cdt`
- [x] При `--dry-run` только показать найденный latest release tag и команду обновления, не выполняя её
- [x] При обычном запуске вызвать `run_self_update()` и завершиться с кодом subprocess
- [x] Обработать случай, когда `pipx` не использовался для установки: вывести инструкцию вручную и завершиться с ошибкой
- [x] Обновить `tests/test_cli.py`: проверить, что `cdt self-update --help` доступна, `--dry-run` возвращает 0 и содержит информацию о версии/команде

### Task 3: Протестировать интеграцию и обработку ошибок

**Files:**
- Modify: `tests/test_self_update.py`
- Modify: `tests/test_cli.py`

- [x] Добавить тест на сетевую ошибку GitHub API (mock `urlopen` выбрасывает `URLError`)
- [x] Добавить тест на отсутствие release-тега в ответе API
- [x] Добавить тест на недетектируемый метод установки (ожидается понятное сообщение и ненулевой exit code)
- [x] Запустить `pytest tests/test_self_update.py tests/test_cli.py` — должен пройти

### Task 4: Verify acceptance criteria

- [x] Запустить полный набор тестов: `pytest` (143 passed)
- [x] Запустить линтер: `ruff check .` (All checks passed)
- [x] Проверить покрытие тестами: `pytest --cov=cdt --cov-report=term` (self_update.py 86%, общее 68% — цель 80%+ достигнута для нового модуля)
- [x] Собрать пакет: `python -m build` (cdt-0.3.0.tar.gz и cdt-0.3.0-py3-none-any.whl)

### Task 5: Update documentation

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] Добавить `cdt self-update` в список команд в `README.md`
- [ ] Добавить `cdt self-update --dry-run` как способ проверить доступность обновления
- [ ] В `CHANGELOG.md` добавить запись под `Unreleased` о новой команде `self-update`
- [ ] Обновить `CLAUDE.md`, если изменились внутренние паттерны

## Acceptance Criteria
- `cdt self-update --help` показывает справку по команде.
- `cdt self-update --dry-run` определяет latest release tag и печатает команду обновления без выполнения.
- `cdt self-update` корректно запускает переустановку через `pipx` из GitHub release tag.
- При ошибках сети/API или неподдерживаемом способе установки выводится понятное сообщение и ненулевой exit code.
- Все тесты проходят, линтер чист.
- README и CHANGELOG обновлены.

## Notes
- Предполагается, что основной способ установки — `pipx install git+https://github.com/Sergionius/cdt.git@vX.Y.Z` (как в README). Если в будущем потребуется поддержка других менеджеров (например, `uv`), её можно добавить в `cdt/self_update.py` без изменения CLI.
- Для editable/local установки команда должна отказаться обновляться и предложить ручную переустановку, чтобы избежать непредсказуемого поведения.
