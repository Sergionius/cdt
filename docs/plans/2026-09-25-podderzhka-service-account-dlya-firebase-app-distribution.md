<!-- ralphex-base: 94d0bb2a688f3eb9fb65645fe7075659e75b54cd -->

# Поддержка service account для Firebase App Distribution

## Goal
Позволить `firebase.upload_app_distribution` загружать сборки через service account без `FIREBASE_TOKEN`, сохранив работу существующих проектов с токеном. Описать настройку на английском языке в `README.md`.

## Context
CDT читает проектный `.env` и затем накладывает переменные окружения терминала, поэтому последние имеют приоритет. Сейчас `cdt/services/firebase.py` обязательно требует `FIREBASE_TOKEN` и добавляет `--token`; метаданные шага и preflight также считают токен обязательным. `cdt/runner.py` запускает Firebase CLI с окружением самого процесса, не передавая значения из `.env` дочернему процессу. Существующие тесты покрывают построение команды, шаг загрузки и JSON preflight.

## Scope
- Два способа авторизации загрузки: существующий `FIREBASE_TOKEN` и путь к ключу service account в `GOOGLE_APPLICATION_CREDENTIALS`.
- Поддержка пути как из `.env`, так и из окружения терминала с существующим приоритетом окружения.
- Обновление preflight, тестов и английского `README.md`.

## Out of Scope
Создание учёток и ключей Google Cloud, выдача IAM-прав, изменение `firebase.deploy` и реальная загрузка сборки.

## Implementation Steps

### Task 1: Добавить второй способ авторизации загрузки

**Files:**
- Modify: `cdt/services/firebase.py`
- Modify: `cdt/steps/firebase.py`
- Modify: `cdt/runner.py`
- Modify: `tests/test_services_firebase.py`
- Modify: `tests/test_steps_firebase.py`
- Modify: `tests/test_runner.py`

- [ ] Сохранить существующую команду с `--token`, если задан непустой `FIREBASE_TOKEN`, в том числе когда одновременно задан путь к ключу.
- [ ] Если токена нет, принимать непустой `GOOGLE_APPLICATION_CREDENTIALS` и строить ту же команду загрузки без `--token`. Если нет обоих способов авторизации, выдавать ошибку с перечислением допустимых вариантов.
- [ ] Добавить в `CommandRunner` необязательную передачу отдельных переменных окружения дочернему процессу. При загрузке через service account передавать выбранное CDT значение `GOOGLE_APPLICATION_CREDENTIALS` в процесс Firebase CLI, не меняя глобальное `os.environ` и не затрагивая другие шаги.
- [ ] Покрыть тестами оба режима, случай с обоими способами авторизации, отсутствие credentials и передачу значения из контекста CDT дочернему процессу.

### Task 2: Согласовать preflight с двумя способами авторизации

**Files:**
- Modify: `cdt/pipeline/builtins.py`
- Modify: `cdt/pipeline/preflight.py`
- Modify: `tests/test_pipeline_plan.py`

- [ ] Оставить `FIREBASE_APP_ID_ANDROID` обязательным для шага загрузки; проверять авторизацию как альтернативу: достаточно `FIREBASE_TOKEN` **или** `GOOGLE_APPLICATION_CREDENTIALS`.
- [ ] Если не задано ни то ни другое, показывать в preflight понятную запись об отсутствии одного из двух способов авторизации. Не менять формат JSON-ответа и правила проверок других шагов.
- [ ] Проверить тестами preflight при токене, пути из `.env`, пути из окружения, отсутствии обоих значений и конфликте `.env` с окружением.

### Task 3: Обновить документацию

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] Добавить в английский `README.md` раздел о Firebase App Distribution: нужная IAM-роль, общий service account для нескольких проектов с выдачей прав в каждом, хранение JSON-ключа вне репозитория, пример `GOOGLE_APPLICATION_CREDENTIALS` для `.env` и окружения терминала.
- [ ] Явно объяснить приоритет окружения над `.env`, приоритет `FIREBASE_TOKEN` над service account, необходимость убрать токен при переходе и проверку через `cdt pipeline preflight <pipeline>`.
- [ ] Кратко отметить новую поддержку в `CHANGELOG.md` без утверждения, что выполнялась реальная загрузка.

## Validation
- `pytest tests/test_services_firebase.py tests/test_steps_firebase.py tests/test_runner.py tests/test_pipeline_plan.py tests/test_commands.py`
- `ruff check .`
- `pytest`

## Acceptance Criteria
- Проект с `FIREBASE_TOKEN` продолжает формировать прежнюю команду с `--token`.
- Проект без токена, но с `GOOGLE_APPLICATION_CREDENTIALS`, формирует команду без `--token`; Firebase CLI получает путь к ключу независимо от того, задан он в `.env` или в окружении терминала.
- При конфликте значений `.env` и окружения побеждает окружение терминала.
- Preflight принимает любой из двух способов авторизации и сообщает об ошибке при отсутствии обоих.
- Английский `README.md` содержит применимые инструкции для одного и нескольких Firebase-проектов.
