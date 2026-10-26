<!-- ralphex-base: f7bb38b7f168f8c47f23869b969a8d6ab5bc9bcb -->

# Разделение CLI CDT на модули без изменения поведения

<!-- plan-slug: modularize-cli-commands -->

## Цель

Превратить `cdt/cli.py` в компактную точку сборки CLI, перенеся обработчики команд в тематические модули `cdt/cli_commands/`. Сохранить существующие команды, параметры, вывод, коды завершения и гарантии безопасности.

## Контекст

- `cdt/cli.py` содержит 1222 строки: регистрацию Typer, служебные команды, настройки, историю, status/logs, запуск, foreground capture, pipeline и agent-release.
- Точка входа установленной команды — `cdt.cli:app` в `pyproject.toml`. `cdt/__main__.py` и многочисленные тесты также импортируют этот объект.
- Используются Typer, pytest, Ruff; CI проверяет Linux/macOS и Python 3.10–3.13, собирает пакет и выполняет smoke-проверку wheel.
- Подкоманды `settings`, `pipeline`, `agent-release` используют `no_args_is_help=True`. Группа `history` имеет callback с `invoke_without_command=True`: без подкоманды показывает историю.
- `run --dry-run` использует общий с `pipeline plan` путь построения плана. Проверка production-подтверждения используется в обычном и detached-запуске.
- Foreground capture управляет записью запуска, блокировкой, итоговым статусом, очисткой истории и Orca-индикацией. Перенос не должен менять порядок этих действий.
- `tests/test_cli.py`, `tests/test_foreground_run.py` и `tests/test_orca_status.py` импортируют или подменяют внутренние символы `cdt.cli`. Их необходимо переключить на новые модули одновременно с переносом.
- Реализация предметной логики уже находится в `cdt/pipeline/`, `cdt/runs.py`, `cdt/retention.py`, `cdt/foreground_run.py` и `cdt/agent_release.py`; рефакторинг их не затрагивает.

## Объём работ

Новая структура:

| Файл | Ответственность |
| --- | --- |
| `cdt/cli.py` | Корневой Typer, callback версии, явная регистрация корневых команд и подключение групп |
| `cdt/cli_commands/__init__.py` | Пакет без побочных действий |
| `cdt/cli_commands/common.py` | Только используемые несколькими модулями CLI-функции: JSON-вывод и проверка необходимости production-подтверждения |
| `cdt/cli_commands/settings.py` | Группа settings и её локальные помощники |
| `cdt/cli_commands/history.py` | Группа history, prune, корневые status/logs и форматирование истории |
| `cdt/cli_commands/pipeline.py` | Группа pipeline, построение плана и локальные функции валидации/отображения |
| `cdt/cli_commands/agent_release.py` | Группа agent-release |
| `cdt/cli_commands/run.py` | Корневой run, его валидация, capture-supervisor и финализация |
| `cdt/cli_commands/utility.py` | Корневые init, doctor, schema, self-update |

Правила зависимостей:

- `cdt/cli.py` импортирует обработчики и группы; дочерние модули не импортируют `cdt.cli`.
- Группы создают собственные Typer-объекты и регистрируют подкоманды локальными декораторами.
- Корневые обработчики не создают дополнительные Typer-приложения: `cdt/cli.py` явно регистрирует их через `app.command(name=...)(handler)`.
- `run.py` использует `_pipeline_plan` из CLI-модуля `pipeline.py`; обратной зависимости нет.
- Общая `_confirmation_required` находится в `common.py`; интерактивное подтверждение остаётся в `run.py`.
- Остальные помощники остаются рядом со своими потребителями. Универсальные регистраторы и контейнеры зависимостей не вводятся.
- Сохраняются существующие порядок регистрации, docstrings обработчиков и явные имена команд.

## Вне объёма работ

- Новые команды, флаги, настройки, изменения JSON-схем или пользовательских сообщений.
- Изменения поведения capture, retention, resume, production-подтверждения и управления процессами.
- Переработка предметных сервисов, SDK, системы плагинов и конфигурации.
- Изменения зависимостей и упаковки, перенос `cdt.cli` в одноимённый пакет.
- Сохранение прежних путей импорта приватных функций и тестовых monkeypatch через совместимые обёртки.

## Шаги реализации

### Task 1: Зафиксировать контракт регистрации CLI

**Files:**
- Modify: `tests/test_cli.py`

- [x] Дополнить существующие проверки точным набором корневых команд, включая `settings`, и подкоманд четырёх групп; проверять отсутствие дублирующей регистрации.
- [x] Зафиксировать существенные параметры Typer: имена и алиасы опций, значения по умолчанию, обязательность аргументов и скрытость `--run-id`/`--capture-child`, используя текущие объявления как исходный контракт.
- [x] Проверить вызовы без аргументов для корня и групп; отдельно сохранить поведение `history` без подкоманды и отсутствие вывода списка истории при `history prune`.
- [x] Сохранить существующие проверки `--version`/`-V` и `python -m cdt`; добавить smoke-проверку существующего пути `python -m cdt.cli --version`.
- [x] Использовать временные проекты и существующий `CliRunner`, не создавать снимки полного Rich-вывода, зависимые от ширины терминала.

### Task 2: Вынести настройки и историю запусков

**Files:**
- Create: `cdt/cli_commands/__init__.py`
- Create: `cdt/cli_commands/common.py`
- Create: `cdt/cli_commands/settings.py`
- Create: `cdt/cli_commands/history.py`
- Modify: `cdt/cli.py`

- [ ] Создать пакет и перенести `_echo_json` в `common.py`; переключить существующих потребителей без изменения сериализации.
- [ ] Перенести `settings_app`, обработчики настроек и их помощники в `settings.py`.
- [ ] Перенести `history_app`, callback истории, prune, status/logs и все локальные помощники форматирования в `history.py`.
- [ ] Подключить перенесённые группы и явно зарегистрировать status/logs в `cdt/cli.py`, сохранив порядок регистрации.
- [ ] Удалить перенесённые определения и неиспользуемые импорты из исходного файла.
- [ ] Проверить неизменность preview/confirm, JSON, stderr и кодов завершения prune существующими тестами истории, retention и настроек.

### Task 3: Вынести команды pipeline и agent-release

**Files:**
- Create: `cdt/cli_commands/pipeline.py`
- Create: `cdt/cli_commands/agent_release.py`
- Modify: `cdt/cli_commands/common.py`
- Modify: `cdt/cli.py`

- [ ] Перенести `pipeline_app`, шесть обработчиков pipeline и все их локальные помощники, включая `_pipeline_plan`, JSON-ошибки, strict warnings и отображение деревьев.
- [ ] Переключить оставшийся в `cdt/cli.py` обработчик run на перенесённую `_pipeline_plan`, сохранив различие между `inputs=None` и пустым словарём.
- [ ] Перенести `_confirmation_required` в `common.py` и подключить её к обоим путям запуска.
- [ ] Перенести `agent_release_app` и обработчики start/status/stop, сохранив структурированные ошибки и код 2 для отсутствующего production-подтверждения.
- [ ] Использовать однозначные импорты предметного `cdt.agent_release`, не спутать его с новым CLI-модулем.
- [ ] Подключить группы из новых модулей и удалить перенесённые определения; не менять порядок загрузки конфигурации, регистрации built-ins и импорта плагинов.

### Task 4: Вынести запуск вместе с тестовыми подменами

**Files:**
- Create: `cdt/cli_commands/run.py`
- Modify: `cdt/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_foreground_run.py`
- Modify: `tests/test_orca_status.py`

- [ ] Перенести `run_pipeline`, capture-константы, валидацию флагов/inputs, интерактивное подтверждение, формирование argv и manifest, supervisor и финализацию в `run.py`.
- [ ] Зарегистрировать обработчик как прежнюю корневую команду `run`, без промежуточных обёрток и изменения сигнатуры.
- [ ] Сохранить ранний выход dry-run, порядок проверок, различение direct/detached/capture-child и передачу всех параметров.
- [ ] Сохранить владение lifecycle lock, запись итоговых статусов, однократную автоочистку и Orca-события; не переносить эти обязанности между предметными слоями.
- [ ] Одновременно обновить импорты `_finalize_captured_run` и monkeypatch для `require_posix_capture`, `release_run_lock`, `report_orca_status`, `run_configured_pipeline` на `cdt.cli_commands.run`.
- [ ] Оставить тестовые вызовы CLI через `from cdt.cli import app`; не ослаблять проверки ради нового расположения функций.

### Task 5: Завершить разделение и проверить точки входа

**Files:**
- Create: `cdt/cli_commands/utility.py`
- Modify: `cdt/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `CHANGELOG.md`

- [ ] Перенести init, doctor, schema, self-update и `_DEFAULT_REPO_URL` в `utility.py`.
- [ ] Оставить в `cdt/cli.py` создание корневого приложения, callback версии, явную регистрацию обработчиков/групп и существующий `__main__` guard.
- [ ] Проверить, что импорты новых модулей не загружают проектную конфигурацию, плагины и пользовательские настройки и не создают записи запусков.
- [ ] Проверить импорт каждого CLI-модуля в свежем процессе, затем импорт корневого приложения: без циклов и повторной регистрации.
- [ ] Удалить оставшиеся зависимости тестов от перенесённых внутренних символов `cdt.cli`, не добавляя совместимых реэкспортов.
- [ ] Добавить краткую запись в `CHANGELOG.md` → `Unreleased` о внутреннем разделении CLI без изменения пользовательского интерфейса.
- [ ] Выполнить полную проверку тестов и сборки; убедиться, что wheel содержит новый пакет и сохраняет точку входа `cdt.cli:app`.

## Проверка

После каждого шага:

```bash
ruff check .
pytest -q tests/test_cli.py
```

Для настроек и истории:

```bash
pytest -q tests/test_user_settings.py tests/test_retention.py tests/test_agent_first.py tests/test_redaction.py
```

Для pipeline, agent-release и запуска:

```bash
pytest -q tests/test_pipeline_plan.py tests/test_agent_release.py tests/test_agent_first.py
pytest -q tests/test_foreground_run.py tests/test_orca_status.py tests/test_pipeline_resume.py tests/test_pipeline_status_file.py tests/test_pipeline_error_ux.py
```

Итоговая проверка:

```bash
ruff check .
pytest -q
python -m compileall -q cdt tests
python -m cdt --version
python -m cdt --help
python -m cdt.cli --version
python -m build
```

Собранный wheel проверить в отдельном временном virtualenv по существующему CI-подходу: установка wheel, `cdt --version`, `cdt --help`, справка четырёх групп и `run`. Реальные релизные пайплайны и внешние публикации для проверки не запускать.

## Критерии приёмки

- `cdt/cli.py` содержит только корневую сборку приложения, callback версии и точку запуска.
- Все обработчики размещены согласно согласованной структуре; нет циклических импортов и автоматического обнаружения модулей.
- `cdt.cli:app`, `python -m cdt` и `python -m cdt.cli` продолжают работать.
- Набор команд, флаги, defaults, help-тексты, JSON, stdout/stderr и exit codes не изменились.
- Сохранились гарантии production-подтверждения, dry-run без побочных действий, capture, resume и retention.
- Тесты подменяют зависимости по месту их нового использования; приватные совместимые обёртки не добавлены.
- Полный pytest, Ruff и сборка пакета проходят; новый пакет присутствует в wheel.
