---
# CI/CD robustness: очистка dist, тихий pytest, retry для tag smoke

## Overview
Довести до конца три CI/CD-улучшения: удаление `dist` перед `python -m build` (защита от stale artifacts), запуск `pytest -q` для сокращения логов, retry c sleep 10 в `github-tag-smoke`. В `pr.yml` и `release.yml` изменения уже присутствуют, осталось применить их в `ci.yml` и убедиться в консистентности.

## Context
- Files involved:
  - `.github/workflows/ci.yml`
  - `.github/workflows/pr.yml`
  - `.github/workflows/release.yml`
- Related patterns: workflows уже используют `rm -rf dist` и `pytest -q`, в `release.yml` уже реализован retry циклом `for attempt in 1 2 3` с `sleep 10`.
- Dependencies: нет новых внешних зависимостей.

## Development Approach
- **Testing approach**: Regular (code first, then tests)
- Complete each task fully before moving to the next
- **CRITICAL: every task MUST include new/updated tests**
- **CRITICAL: all tests must pass before starting next task**

## Implementation Steps

### Task 1: Добавить rm -rf dist перед build в ci.yml

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] заменить шаг `Build` с `run: python -m build` на многострочную команду:
  ```yaml
  rm -rf dist
  python -m build
  ```
- [ ] запустить `python -m pytest -q` локально
- [ ] запустить `ruff check .` локально
- [ ] убедиться, что `rm -rf dist && python -m build` работает локально

### Task 2: Привести вызовы pytest к -q

**Files:**
- Modify: `.github/workflows/*.yml` (при необходимости)

- [ ] проверить все `pytest`-команды в workflows
- [ ] убедиться, что в `pr.yml`, `ci.yml`, `release.yml` используется флаг `-q`
- [ ] запустить `python -m pytest -q` локально для подтверждения
- [ ] запустить `ruff check .` локально

### Task 3: Проверить retry в github-tag-smoke

**Files:**
- Modify: `.github/workflows/release.yml` (при необходимости)

- [ ] проверить job `github-tag-smoke`: `for attempt in 1 2 3`, `sleep 10`, `exit 1` после последней попытки
- [ ] убедиться, что логика retry покрывает установку из git tag
- [ ] запустить локально аналогичную команду `pip install` для проверки синтаксиса шага

### Task 4: Verify acceptance criteria

- [ ] запустить полный локальный test suite: `python -m pytest -q`
- [ ] запустить линтер: `python -m ruff check .`
- [ ] проверить сборку после очистки: `rm -rf dist && python -m build`
- [ ] запустить `twine check dist/*` (если установлен)

### Task 5: Update documentation

- [ ] при необходимости добавить/обновить раздел в CLAUDE.md о порядке CI-шагов
- [ ] проверить, что CHANGELOG.md не требует обновления для этих внутренних изменений
---
