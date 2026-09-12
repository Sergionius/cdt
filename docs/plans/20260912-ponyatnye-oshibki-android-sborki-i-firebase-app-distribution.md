<!-- ralphex-base: 32e0d5d1d6b46c03e018e36520d726045bc5d7c3 -->

# Понятные ошибки Android-сборки и Firebase App Distribution

## Goal

Сохранить команду и настоящий код завершения при ошибках Android/Firebase, показать конкретный упавший шаг и объяснить, где искать причину, без `unknown`, `Invalid value` и CLI usage.

## Context

- В присланном сообщении упала загрузка в Firebase, а не обязательно Android-сборка. `Failed to make request` без дополнительных диагностик не доказывает проблему токена, прав, сети или самого артефакта.
- `cdt/pipeline/executor.py` уже содержит `PipelineExecutionError`, вывод ошибок дочерних шагов и поддержку `CommandExecutionError`. `cdt/cli.py` уже выводит такие ошибки без usage. Старый текст из запроса отсутствует в текущей реализации; версия установленного у пользователя CDT неизвестна.
- `cdt/steps/ios.py` уже сохраняет команду и код Flutter через `CommandExecutionError`.
- `cdt/steps/firebase.py` и `cdt/steps/android.py` пока заменяют любой ненулевой результат на `typer.Exit(code=1)`, теряя диагностические данные.
- `cdt/runner.py` возвращает integer exit code, а при ошибке в невverbose-режимах печатает хвост вывода и путь полного временного лога. Текущая инфраструктура сохраняет редактированную итоговую ошибку в run log и status.

## Scope

- Ошибки `firebase.upload_app_distribution`, `android.build_aab`, `android.build_apk`.
- Регрессионные проверки последовательного и параллельного выполнения, CLI и сохранённых результатов.
- Документация диагностики и changelog.

## Out of Scope

- Исправление недоказанной внешней причины запроса Firebase, реальные загрузки и использование credentials.
- Автоматические повторы, включение Firebase `--debug`, изменение аутентификации или аргументов загрузки.
- `firebase.deploy`, legacy upload helpers, переработка runner, схемы статуса или executor.
- Обещание, что улучшенная диагностика устранит сетевую ошибку.

## Implementation Steps

### Task 1: Сохранить данные ошибки загрузки Firebase
**Files:**
- Modify: `cdt/steps/firebase.py`
- Modify: `tests/test_steps_firebase.py`

- [x] В `FirebaseUploadAppDistributionStep.run` сохранить результат единственного вызова runner в `exit_code`. При ненулевом результате воспроизвести существующий fail sound и поднять `CommandExecutionError` с исходным списком аргументов и реальным кодом.
- [x] Использовать причину: `Firebase App Distribution upload failed. Check the Firebase CLI output above for details; inspect the saved run with cdt logs <run-id>.` Не добавлять предположений о HTTP-статусе, токене или сети.
- [x] Оставить неизменными выбор артефакта, release notes, построение команды, cwd, успешное сообщение и поведение `FirebaseDeployStep`.
- [x] Заменить тест ожидания `typer.Exit` проверками `CommandExecutionError`: параметризовать ненулевые коды 1 и 7, проверить cause, точное совпадение command с вызовом runner, сохранение exit code и один fail sound.
- [x] Дополнить успешный сценарий проверками единственного вызова runner, отсутствия fail sound и прежнего сообщения успеха.

### Task 2: Применить существующий iOS-подход к Android-сборкам
**Files:**
- Modify: `cdt/steps/android.py`
- Modify: `tests/test_android.py`

- [x] В обоих build steps вычислять команду один раз, сохранять возвращённый код и вместо `typer.Exit(code=1)` поднимать `CommandExecutionError`, сохранив существующий fail sound.
- [x] Для AAB использовать причину `Android AAB build failed. Check the Flutter/Gradle output above for details.`, для APK — `Android APK build failed. Check the Flutter/Gradle output above for details.`
- [x] Не менять build options и регистрацию артефактов: выполнять регистрацию только после успешного завершения команды.
- [x] Добавить параметризованные unit-тесты обоих шагов с fake runner: точная команда и cwd, сохранение кода 7, один fail sound, отсутствие регистрации артефакта после ошибки.
- [x] Добавить успешные проверки обоих шагов с временными файлами ожидаемого формата: регистрация именованного артефакта и отсутствие fail sound.

### Task 3: Зафиксировать диагностику Firebase через executor и CLI
**Files:**
- Modify: `tests/test_pipeline_error_ux.py`

- [x] По существующему iOS-прецеденту добавить временный YAML-проект с `android.build_aab`, затем параллельной группой: `firebase.upload_app_distribution` и успешный sibling. Использовать фиктивные app ID, token, артефакты и runner без внешних процессов.
- [x] Синхронизировать fake runner через `threading.Event`: Firebase возвращает 7, sibling завершается после сигнала ошибки. Проверить, что sibling действительно разрешено закончить.
- [x] В CLI-тесте проверить leaf ID и имя Firebase-шагa, понятную причину, фактическую команду с путём артефакта и `--token ***`, `Exit code: 7`, уведомление о завершении siblings и итоговый CLI exit code 1.
- [x] Проверить отсутствие `Usage:`, `Invalid value`, старого сообщения `Parallel group failed after all steps finished` и `unknown`.
- [x] Проверить созданный run record: `status=failed`, корректный `failed_step`, завершённый sibling, сохранённый ранее собранный Android-артефакт, `exit-code` равен `1`, одинаковые существенные диагностические данные в terminal, `status.json` и `output.log`. Секрет не должен присутствовать ни в одном из этих представлений.
- [x] Добавить executor-тест того же реального Firebase step без parallel group: ошибка оборачивается в `PipelineExecutionError`, сохраняет command/exit code и не получает параллельное уведомление.
- [x] Добавить случай Firebase upload внутри последовательной ветки parallel group: итоговый `failed_step` указывает на вложенный upload, а не на sequence или parallel.
- [x] Добавить параметризованную executor-проверку реальных Android AAB/APK steps: ошибки содержат правильное имя, причину, команду и исходный код завершения.
- [x] Оставить действующими проверки validation UX и iOS-сценариев; не менять общий формат executor ради платформенных сообщений.

### Task 4: Документировать границы диагностики
**Files:**
- Modify: `docs/runs.md`
- Modify: `CHANGELOG.md`

- [x] Расширить раздел `Failed-build output` примером Firebase upload failure с фиктивными идентификаторами, замаскированным токеном и сохранённым кодом Firebase CLI.
- [x] Объяснить различие между ошибкой Android build и последующей загрузки. Указать, что `Failed to make request` само по себе не устанавливает первопричину; подробности следует искать в Firebase output и через `cdt logs <run-id> --tail 80`.
- [x] Сохранить описанное ограничение direct verbose execution: raw subprocess output не обязательно попадает в run log. Не обещать автоматическое извлечение HTTP/network-причины или безопасность сторонних debug logs.
- [x] Уточнить, что `Artifacts produced: none` относится к текущему шагу/группе, а не означает отсутствие ранее собранного AAB; полный список артефактов хранится в status.
- [x] Заменить `Nothing yet` в `Unreleased` краткой записью о сохранении команд и кодов завершения Android/Firebase, без заявления об исправлении доступности Firebase.

## Validation

Все проверки используют временные проекты и fake runners, без реальных build/upload-команд.

Focused tests:

```bash
pytest tests/test_steps_firebase.py tests/test_services_firebase.py tests/test_android.py tests/test_commands.py tests/test_pipeline_error_ux.py
pytest tests/test_pipeline_executor.py tests/test_pipeline_status_file.py tests/test_pipeline_resume.py tests/test_runner.py tests/test_redaction.py
```

Общие проверки:

```bash
pytest
ruff check .
```

## Acceptance Criteria

- Firebase upload failure содержит конкретный leaf step, понятный контекст загрузки, реальную команду с замаскированным токеном и исходный exit code.
- AAB/APK build failures сохраняют аналогичные данные, различая сборку и загрузку.
- CLI runtime error не выглядит ошибкой аргументов; CLI по-прежнему завершается кодом 1.
- Параллельные siblings заканчивают работу; статусы и уже зарегистрированные артефакты сохраняются.
- Terminal summary, persisted error и run log не раскрывают тестовый Firebase token.
- Успешные команды, конфигурация, схемы и поведение validation errors не меняются.
- Документация не выдаёт предполагаемую причину внешнего сбоя за установленный факт.

## Execution Notes

- Decision: исправить потерю диагностических данных, не объявлять внешнюю причину Firebase установленной; Alternatives: считать причиной сеть, credentials или Android-артефакт; Reason: предоставленный текст не различает эти причины; Side effects: upload может продолжать падать, но итоговая ошибка станет информативнее.
- Decision: включить AAB/APK steps вместе с Firebase upload; Alternatives: исправить только upload либо все built-ins; Reason: пользователь отдельно упоминает Android, а оба build steps имеют тот же локальный дефект относительно iOS-прецедента; Side effects: три шага вместо `typer.Exit` поднимают существующий structured exception.
- Decision: использовать существующий `CommandExecutionError` и текущий executor без нового API; Alternatives: изменить return type runner или добавить сбор stderr; Reason: нужная инфраструктура уже реализована и покрыта тестами; Side effects: нет изменений публичной конфигурации и статуса.
- Decision: оставить сообщения CLI на английском; Alternatives: русские сообщения или локализация; Reason: действующий runtime UX и документация используют английский; Side effects: none.
- Decision: не включать debug и автоматические retries; Alternatives: повторять upload или собирать расширенный Firebase trace; Reason: это меняет внешнее поведение и может раскрыть credentials без доказанной необходимости; Side effects: none.
- Decision: использовать существующую редактирующую инфраструктуру и проверить Firebase token во всех итоговых представлениях; Alternatives: менять глобальный redactor или скрывать всю команду; Reason: `FIREBASE_TOKEN` уже определяется как credential, а команда полезна для диагностики; Side effects: сохраняются текущие ограничения стороннего raw output.
- Decision: проверять сценарии только offline через существующие pytest conventions; Alternatives: воспроизведение загрузки в реальный Firebase; Reason: для исправления передачи ошибок credentials и внешние операции не нужны; Side effects: фактическая доступность Firebase не проверяется.
- Decision (Task 1): запускать тесты через `.venv/bin/pytest`, так как `pytest` отсутствует в PATH данной среды; Alternatives: установка pytest глобально; Reason: проект уже содержит виртуальное окружение со всеми зависимостями; Side effects: none.
- Decision (Task 1): в тестах дополнительно зафиксировать cwd вызова runner, единственность вызова и фрагменты команды (`appdistribution:distribute`, путь артефакта, `TASK-1`); Alternatives: ограничиться минимальными проверками из чекбоксов; Reason: требование «точное совпадение command с вызовом runner» и «единственный вызов runner» явно сформулировано в чекбоксах; Side effects: none.
- Decision (Task 2): убрать ставший неиспользуемым `import typer` из `cdt/steps/android.py`; Alternatives: оставить импорт; Reason: после удаления `typer.Exit` модуль больше не использует typer, это единственное затронутое место; Side effects: none.
- Decision (Task 2): в тестах сравнивать переданную команду с результатом существующих билдеров `_build_android_aab_command()`/`_build_android_apk_command()` вместо дублирования литерального списка аргументов; Alternatives: захардкодить полный ожидаемый command; Reason: чекбокс требует точную команду и cwd, билдеры покрыты собственными тестами и отражают неизменность build options; Side effects: none.
- Decision (Task 2): в failure-тестах параметризовать только код 7, а в success-тестах проверять kind/label/path зарегистрированного артефакта через `ctx.artifact(name)`; Alternatives: параметризация нескольких кодов как в Task 1; Reason: чекбокс Task 2 явно требует сохранение кода 7 и регистрацию именованного артефакта; Side effects: none.
- Decision (Task 3): в качестве успешного sibling в parallel group использовать реальный `android.build_apk`; Alternatives: кастомный demo-шаг или шаг без runner; Reason: сохраняет iOS-прецедент с одними builtin-шагами и дополнительно проверяет, что параллельная ветка реально выполняет команду и регистрирует артефакт; Side effects: в статусе сохраняются оба артефакта (android_aab и android_apk).
- Decision (Task 3): fake runner различает команды по префиксам (`firebase appdistribution:distribute` возвращает 7, `flutter build apk` ждёт `threading.Event`, остальные возвращают 0); Alternatives: синхронизация по счётчику вызовов; Reason: последовательный `android.build_aab` идёт до отказа Firebase, поэтому ожидание ошибки должно применяться только к sibling внутри parallel group; Side effects: none.
- Decision (Task 3): в CLI-тесте удалить `FIREBASE_APP_ID_ANDROID`/`FIREBASE_TOKEN`/`FIREBASE_GROUPS` из `os.environ` через `monkeypatch.delenv`; Alternatives: полагаться только на `.env`; Reason: `_load_project_env` отдаёт приоритет переменным окружения, и значения из среды разработчика могли бы подменить фикстуру; Side effects: none.
- Decision (Task 3): в executor-тестах создавать шаги напрямую и присваивать `step_id` атрибутом, как в существующих тестах файла; Alternatives: регистрация фабрик в registry и использование `ConfiguredStep`; Reason: соответствует локальным convention и не требует дополнительной настройки registry; Side effects: none.
- Decision (Task 4): пример Firebase failure в `docs/runs.md` использует фиктивные идентификаторы (app ID `1:1234567890:android:0a1b2c3d4e5f6a7b`, путь `/path/to/project/...`, exit code 7, sibling-артефакт `android_apk`) и формат, дословно совпадающий с выводом executor и тестов Task 1–3; Alternatives: перенос реальных идентификаторов из запроса; Reason: реальные app ID, пути и токены не должны попадать в публичную документацию; Side effects: none.
- Decision (Task 4): абзацы о различии сборки и загрузки, `Failed to make request` и `Artifacts produced` добавлены смежными абзацами внутри `Failed-build output`, а ограничение direct verbose execution — в `Logs and secret redaction`, где это ограничение уже описано; Alternatives: новые отдельные секции или дублирование ограничения; Reason: сохраняет структуру документа и единственное место описания ограничения логирования; Side effects: none.
- Decision (Task 4): ограничение дополнено явным предупреждением не включать `firebase --debug` в пайплайн из-за возможной утечки credential-материала, которую redactor не гарантирует классифицировать; Alternatives: промолчать о debug-режиме; Reason: чекбокс требует не обещать безопасность сторонних debug logs, а это самый вероятный обходной путь пользователя; Side effects: none.
- Decision (Task 4): документация-only задача провалидирована сверкой цитат, команды `cdt logs <run-id> --tail 80` и формата summary с `cdt/pipeline/executor.py`, `cdt/steps/firebase.py`, `cdt/steps/android.py`, `cdt/cli.py` и тестами Task 1–3 вместо запуска pytest; Alternatives: полный тестовый прогон из Validation; Reason: инструкции воркера запрещают compile/test/build для documentation-only задач; Side effects: none.
