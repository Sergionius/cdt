<!-- ralphex-base: 3512d2552294e006567a49490f7e2a2bf6655c05 -->

# Стабилизация загрузки TestFlight и возобновления ASC-фазы

## Goal

Сделать интеграцию App Store Connect устойчивой к кратковременным сетевым сбоям и истечению JWT, отделить загрузку IPA от возобновляемой post-upload обработки и улучшить диагностику запусков. После успешного `iTMSTransporter` повторный запуск post-upload фазы должен находить уже загруженный build, дожидаться `VALID` и выставлять changelog без повторной загрузки IPA и изменения build number.

## Context

В CDT 0.4.1 `appstore.upload_testflight` выполняет две операции внутри одного шага: запускает `iTMSTransporter`, затем вызывает `_complete_testflight_after_upload()`. Последняя создаёт один JWT, находит приложение, опрашивает `/v1/builds` через `_asc_wait_build()` и обновляет TestFlight changelog.

Текущие ограничения:

- `cdt/services/appstore.py::_asc_request()` преобразует только `HTTPError`; timeout, `URLError`, SSL EOF и reset соединения не повторяются.
- JWT создаётся один раз на всю post-upload обработку, хотя ожидание ASC может быть дольше его 20-минутного срока.
- Resume работает на уровне pipeline-шагов. Повтор failed `appstore.upload_testflight` снова запускает transporter.
- Для совместимости существующий `appstore.upload_testflight` должен сохранить полный цикл.
- Новая рекомендуемая конфигурация будет использовать `appstore.upload_testflight_ipa` и `appstore.complete_testflight` как последовательные шаги.
- Parallel executor знает ошибку дочернего шага, но верхнеуровневая сводка описывает только группу и выводит `command: unknown; exit code: unknown`.
- Direct run создаёт `output.log`, но в отличие от detached worker не записывает туда CDT-вывод.
- Логи и status-файлы должны продолжать проходить существующую redaction.
- Полный release pipeline нельзя использовать для проверки, поскольку он изменяет build number и повторно загружает артефакты.

## Scope

- Retry с bounded exponential backoff и jitter для временных ошибок ASC.
- Поддержка `Retry-After` для HTTP 429 и временных 5xx.
- Немедленный отказ для постоянных 4xx.
- Автоматическое обновление JWT по возрасту и однократное принудительное обновление при 401.
- Отдельные встроенные шаги `appstore.upload_testflight_ipa` и `appstore.complete_testflight`.
- Сохранение обратной совместимости `appstore.upload_testflight`.
- Полезная дочерняя ошибка в результате parallel group.
- Redacted `output.log` для direct и detached запусков.
- Документация общей миграции pipeline на раздельные TestFlight-фазы.
- Безопасная проверка на уже загруженном build без сборки и повторного upload.

## Out of Scope

- Автоматическое изменение пользовательских `cdt.yaml`.
- Изменение формата ASC credentials.
- Повторный запуск transporter после подтверждённого успешного upload.
- Автоматический rollback build number или удаление build из App Store Connect.
- Полный тестовый или production release в качестве проверки исправления.

## Implementation Steps

### Task 1: Устойчивый клиент App Store Connect

**Files:**
- Modify: `cdt/services/appstore.py`
- Modify: `tests/test_services_appstore.py`

- [x] Инкапсулировать получение и кэширование JWT для ASC-запросов так, чтобы токен обновлялся до истечения 20 минут и мог быть принудительно обновлён после ответа 401.
- [x] Перевести `_asc_request()`, `_asc_get_app_id()`, `_asc_wait_build()` и `_asc_set_changelog()` на единый token-aware клиент, не передающий один неизменяемый JWT через весь polling.
- [x] Добавить в `_asc_request()` ограниченное число повторов с exponential backoff, верхней границей задержки и jitter; вынести параметры и вычисление задержки в тестируемые приватные функции или константы без добавления лишних публичных настроек.
- [x] Считать временными `URLError` с сетевой причиной, `TimeoutError`, `socket.timeout`, SSL EOF, reset/abort соединения и соответствующие временные `OSError`; после исчерпания retry budget возвращать понятную redacted-совместимую ошибку с числом попыток и последней причиной.
- [x] Повторять HTTP 429 и временные ответы 5xx, учитывая `Retry-After` как число секунд или HTTP-date и ограничивая фактическую задержку заданной верхней границей.
- [x] Для 401 один раз принудительно обновлять JWT и повторять запрос; повторный 401 и остальные постоянные 4xx немедленно преобразовывать в `typer.BadParameter` с HTTP status и безопасным телом ответа.
- [x] Не позволять retry-задержкам polling-запроса бесконтрольно выходить за общий deadline `_asc_wait_build()`; логировать номер попытки, категорию временного сбоя и следующую задержку без credentials и authorization headers.
- [x] Покрыть unit-тестами успешный запрос, все временные классы ошибок, исчерпание попыток, отсутствие retry для постоянного 4xx, 429/5xx, оба формата `Retry-After`, jitter/backoff bounds, refresh до expiry и refresh-on-401.

### Task 2: Раздельные возобновляемые TestFlight-фазы

**Files:**
- Modify: `cdt/services/appstore.py`
- Modify: `cdt/steps/appstore.py`
- Modify: `cdt/pipeline/builtins.py`
- Modify: `cdt/cdt.schema.json`
- Modify: `tests/test_services_appstore.py`
- Modify: `tests/test_flows_ios.py`
- Modify: `tests/test_pipeline_registry.py`
- Modify: `tests/test_agent_first.py`

- [x] Разделить service-level операции на upload-only функцию для `iTMSTransporter` и `_complete_testflight_after_upload()` для поиска build, ожидания terminal processing state и идемпотентной установки changelog.
- [x] Оставить `_upload_testflight()` совместимым оркестратором полного цикла: post-upload обработка запускается только после нулевого exit code transporter.
- [x] Добавить built-in `appstore.upload_testflight_ipa`, принимающий IPA artifact и выполняющий только transporter upload.
- [x] Добавить built-in `appstore.complete_testflight`, не требующий IPA и использующий `ctx.new_version`, `IOS_BUNDLE_ID` и ASC credentials для обработки уже загруженного build.
- [x] Сделать changelog-фазу идемпотентной: существующая `en-US` localization обновляется через PATCH, отсутствующая создаётся через POST; повтор completion не выполняет upload.
- [x] Зарегистрировать новые шаги и metadata: upload-only шаг требует `ios_ipa` и `xcrun`, completion-шаг требует ASC environment и version context, но не artifact и не transporter.
- [x] Перегенерировать bundled JSON Schema через существующий `schema_payload()` и проверить соответствие публичных опций новых шагов.
- [x] Добавить тесты полного совместимого шага, upload-only шага, completion-only шага, отсутствующей версии, transporter failure и повторной completion для уже существующего build.
- [x] Добавить интеграционный тест resume: status с завершённым upload-only шагом и failed completion восстанавливает `new_version`, запускает только `appstore.complete_testflight` и ни разу не вызывает transporter или increment step.

### Task 3: Сохранение причины ошибки дочернего parallel-шагa

**Files:**
- Modify: `cdt/pipeline/executor.py`
- Modify: `tests/test_pipeline_executor.py`
- Modify: `tests/test_pipeline_error_ux.py`

- [x] Сохранять для каждого failed future идентификатор, имя шага, исходное исключение и доступное описание команды вместо агрегации только имён.
- [x] Формировать верхнеуровневую ошибку parallel group с реальной причиной каждого failed child; для одного сбоя явно показывать его step id/name и сообщение исключения.
- [x] Передавать metadata фактически упавшего child в общую failed-step сводку, чтобы она не приписывала ошибку абстрактному шагу `parallel`.
- [x] Использовать осмысленное обозначение built-in шага и `not applicable` вместо `command: unknown` и `exit code: unknown`, когда ошибка не относится к subprocess exit code.
- [x] Сохранить ожидание завершения остальных parallel branches, `failed_step`, `parallel_failed`, redaction и поддержку нескольких одновременных ошибок.
- [x] Добавить тесты одиночной и множественной parallel-ошибки, дочерней команды, сетевого исключения без exit code и отсутствия секретов в итоговом status.

### Task 4: Диагностический output.log для direct run

**Files:**
- Modify: `cdt/redaction.py`
- Modify: `cdt/runs.py`
- Modify: `cdt/pipeline/runner.py`
- Modify: `tests/test_agent_first.py`
- Modify: `tests/test_redaction.py`
- Modify: `tests/test_pipeline_status_file.py`

- [x] Добавить run-scoped потокобезопасный tee/recorder, который сохраняет CDT-owned stdout/stderr в `output.log`, продолжая выводить их в текущий терминал.
- [x] Подключать recorder после создания run record для обычного `cdt run`, но не дублировать вывод внутри detached worker, который уже захватывает объединённый subprocess stream.
- [x] Применять `StreamingRedactor` только к сохраняемой копии, оставляя текущую интерактивную семантику терминала и гарантируя flush остатка при успехе, исключении и interrupt.
- [x] Явно записывать в лог terminal summary исключения до повторного выброса, чтобы сетевой сбой оставался диагностируемым даже если Typer форматирует финальную ошибку за пределами run context.
- [x] Обеспечить корректную конкурентную запись сообщений parallel branches и отсутствие JWT, authorization headers и известных environment secrets в файле.
- [x] Добавить direct-run тесты непустого success/failure лога, сохранения ASC retry diagnostics, terminal error summary, redaction и отсутствия двойных строк в detached execution.

### Task 5: Документация миграции и релизные заметки

**Files:**
- Modify: `README.md`
- Modify: `docs/pipelines.md`
- Modify: `docs/runs.md`
- Modify: `examples/cdt.yaml`
- Modify: `CHANGELOG.md`

- [ ] Описать совместимость `appstore.upload_testflight` и рекомендованную sequence-конфигурацию из `appstore.upload_testflight_ipa` и `appstore.complete_testflight`.
- [ ] Привести общий пример resume из failed status, который пропускает завершённые build/upload шаги и начинает с `appstore.complete_testflight`.
- [ ] Уточнить, что completion повторно ищет build по сохранённому `new_version`, не загружает IPA и не изменяет build number.
- [ ] Документировать retry-классификацию, `Retry-After`, обновление JWT, общий `ASC_WAIT_TIMEOUT_SEC` и диагностические сообщения без обещания retry постоянных 4xx.
- [ ] Обновить описание `output.log`: direct run сохраняет redacted CDT diagnostics, detached run продолжает сохранять redacted combined output.
- [ ] Добавить запись в `CHANGELOG.md` об устойчивости ASC, новых resumable шагах, улучшенных parallel errors и direct-run logging.

## Validation

В репозитории CDT:

```bash
pytest tests/test_services_appstore.py
pytest tests/test_pipeline_executor.py tests/test_pipeline_error_ux.py
pytest tests/test_pipeline_resume.py tests/test_agent_first.py tests/test_redaction.py tests/test_pipeline_status_file.py
pytest
ruff check .
python -m build
```

Безопасная проверка интеграции должна выполняться без полного release pipeline:

1. Сначала read-only запросом ASC найти ранее загруженный build 726 и проверить его `processingState`.
2. Проверить план completion-only pipeline через `cdt run <completion-pipeline> --dry-run`.
3. Если changelog ещё не выставлен, запустить только `appstore.complete_testflight` с `new_version`, восстановленным из status failed run.
4. Убедиться по stub/spy либо изолированному сценарию, что transporter и increment step не запускались.
5. Не выполнять полный `cdt run test` и не собирать новый IPA для этой проверки.

## Acceptance Criteria

- Timeout, SSL EOF, connection reset и аналогичный временный сетевой сбой во время ASC polling повторяются с bounded backoff и не валят релиз при последующем восстановлении сети.
- HTTP 429 и временные 5xx учитывают `Retry-After`; постоянные 4xx не повторяются.
- Долгое ожидание использует обновлённый JWT, а первый 401 приводит к refresh без повторной загрузки IPA.
- Существующий `appstore.upload_testflight` сохраняет прежний полный цикл.
- Новая последовательность `appstore.upload_testflight_ipa` → `appstore.complete_testflight` поддерживает resume только с completion-фазы.
- Resume на уже загруженном build не запускает transporter и не изменяет build number.
- Completion находит существующий build, дожидается `VALID` и создаёт либо обновляет changelog.
- Итоговая parallel-ошибка содержит step id/name и исходную причину дочернего сбоя без `command: unknown; exit code: unknown`.
- `output.log` direct и detached запусков содержит полезную redacted диагностику.
- Полный набор тестов, lint и package build завершаются успешно.

## Execution Notes

Task 1 — Устойчивый клиент App Store Connect (autonomous decisions):

- Retry параметры вынесены в модульные константы `cdt/services/appstore.py` (`ASC_RETRY_MAX_ATTEMPTS=4`, база 1s, верхняя граница 15s, jitter ≤0.5s, таймаут запроса 60s, запас обновления JWT 60s): план запрещает новые публичные настройки, константы остаются тестируемыми напрямую.
- `_AscClient` кэширует JWT и обновляет его при приближении к 20-минутному сроку (за 60s); `force_token_refresh()` вызывается не более одного раза на вызов `_asc_request()` при 401, обновлённый токен остаётся закэшированным для последующих запросов. Повторный 401 немедленно даёт `typer.BadParameter` со статусом и телом ответа.
- Классификация временных сбоев: любой `URLError` считается временным (его не-HTTP причина описывает сетевой сбой; `HTTPError` обрабатывается отдельной веткой раньше), плюс `TimeoutError`/`socket.timeout`, `ssl.SSLError` (включая SSL EOF), подклассы `ConnectionError` (reset/abort/remote-disconnected) и `OSError` с известными временными errno; остальные `OSError` завершают запрос сразу без retry.
- Retry-задержки ограничиваются дедлайном `_asc_wait_build()` через `client.deadline` (после истечения дедлайна задержка 0; внешний polling-цикл завершается по собственной проверке дедлайна).
- Диагностика retry выводится через `typer.echo` в обоих UI-режимах: номер попытки, категория сбоя и следующая задержка; credentials и authorization headers не логируются.
- Безопасная интеграционная проверка на реальном ASC (build 726, dry-run completion-only pipeline) не выполнялась: она опирается на шаг `appstore.complete_testflight`, появляющийся в Task 2, и требует живых ASC credentials. Валидация Task 1 покрыта unit-тестами (46 passed), полным `pytest` (371 passed), `ruff check .` и `python -m build`.

Task 2 — Раздельные возобновляемые TestFlight-фазы (autonomous decisions):

- Upload-only service-функция названа `_upload_testflight_ipa(ipa_path, env)`; `_upload_testflight()` теперь делегирует ей transporter-фазу, сохраняя прежний контракт (non-zero exit transporter → возврат кода без post-upload обработки).
- `CompleteTestFlightStep` использует тот же `ChangelogProvider` и значение по умолчанию `"dev build"`, что и совместимый `UploadTestFlightStep`; upload-only шаг не имеет опции changelog, так как она не нужна transporter-фазе.
- Metadata: `appstore.upload_testflight_ipa` требует `ios_ipa`, `xcrun` и три ASC-ключа (transporter не нуждается в `IOS_BUNDLE_ID`); `appstore.complete_testflight` требует все четыре ASC-ключа, но не artifact и не `xcrun`. Требование version context (`new_version`) в metadata выразить нечем — оно проверяется в рантайме (`Missing pipeline value: new_version`) и описано в description шага.
- `appstore.upload_testflight_ipa` объявляет `produces: upload_result` (зеркально совместимому шагу); completion-шаг ничего не производит, так как upload уже выполнен.
- Идемпотентность changelog уже обеспечивалась `_asc_set_changelog()` (GET → PATCH существующей `en-US` localization, POST отсутствующей); добавлены тесты повторного completion без upload на service- и step-уровне.
- Безопасная проверка из Validation (ASC build 726) не выполнялась — требует живых ASC credentials; взамен по пункту 4 Validation сделан stub/spy интеграционный тест resume (`test_resume_skips_finished_upload_and_reruns_only_testflight_completion`), доказывающий, что при resume не запускаются transporter и increment step, а `new_version` восстанавливается из status. Пункт 2 (dry-run completion-only pipeline) проверен локально на временном проекте без credentials.
- Валидация Task 2: `pytest tests/test_services_appstore.py` (49 passed), `pytest tests/test_pipeline_executor.py tests/test_pipeline_error_ux.py` (12 passed), `pytest tests/test_pipeline_resume.py tests/test_agent_first.py tests/test_redaction.py tests/test_pipeline_status_file.py` (40 passed), полный `pytest` (381 passed), `ruff check .`, `python -m build` (bundled schema с новыми шагами попала в wheel).

Task 3 — Сохранение причины ошибки дочернего parallel-шага (autonomous decisions):

- Каждый failed future описывается dataclass `ChildFailure` (step id, имя шага, исходное исключение, описание команды), а не парой `(label, exception)`; для nested sequence-веток через `ctx.parallel_failed` разрешается самый глубокий фактически упавший шаг и его metadata (`_describe_child_failure` / `_find_step_by_id`).
- Формат верхнеуровневой ошибки: `Parallel group failed after all steps finished: <id> (<name>): <причина>; ...` — единый для одиночного и множественного сбоя; префикс сохранён, чтобы не ломать существующие ожидания resume-логов и тестов.
- Первичный child для failed-step сводки выбирается тем же `_deepest_failed_step_id()`, что и `failed_step`, и передаётся через атрибуты исключения `failed_step_label` / `failed_step_command`; executor применяет их только если они выставлены (non-parallel шаги работают как раньше).
- `command: unknown` заменён на `command: not applicable (built-in step <label>)`, а `exit code: unknown` — на `exit code: not applicable`; exit code по-прежнему извлекается из текста ошибки subprocess-сбоев.
- Поведение ожидания всех веток, `failed_step`, `parallel_failed`, redaction и множественных ошибок сохранено; итоговое сообщение redact'ится executor'ом как раньше, status-файл дополнительно проходит `redact_data`.
- Безопасная ASC-интеграция из Validation к Task 3 неприменима: задача не затрагивает ASC-код и требует живых credentials; взамен поведение покрыто unit-тестами. Валидация Task 3: `pytest tests/test_pipeline_executor.py tests/test_pipeline_error_ux.py` (18 passed), `pytest tests/test_services_appstore.py` (49 passed), `pytest tests/test_pipeline_resume.py tests/test_agent_first.py tests/test_redaction.py tests/test_pipeline_status_file.py` (40 passed), полный `pytest` (387 passed), `ruff check .`, `ruff format --check`, `python -m build`.

Task 4 — Диагностический output.log для direct run (autonomous decisions):

- Run-scoped tee/recorder реализован в `cdt/runs.py` (`RunOutputRecorder` + `_TeeStream`) как run-инфраструктура рядом с `RunPaths`; `cdt/redaction.py` остался без изменений — существующие `StreamingRedactor`/`SecretRedactor` уже покрывают требования (feed с удержанием незавершённой строки, flush остатка через `feed(..., final=True)`, паттерны JWT/Authorization/assignment), recorder лишь компонует их под общим `threading.Lock`.
- Recorder подключается в `run_configured_pipeline` сразу после создания run record и pid-файла только при `not detached`: worker всегда передаёт `--run-id`, поэтому во detached-исполнении recorder не ставится и строки не дублируются (покрыто тестом `test_detached_execution_does_not_duplicate_output_lines` с подсчётом вхождений маркера).
- Лог открывается лениво в режиме append внутри lock'а; при `OSError` recorder отключает себя (fail-open), терминальные потоки оборачиваются только если лог доступен — диагностика никогда не ломает запуск; `close()` восстанавливает `sys.stdout`/`sys.stderr`, сбрасывает redaction-остаток и идемпотентен.
- Terminal summary исключения пишется в формате `CDT run failed: <ExceptionType>: <message>` через redacting recorder до повторного выброса; для исключений без сообщения (например, `KeyboardInterrupt`) выводится только имя типа. Ошибки `_restore_resume_status` сохраняют прежнее поведение (exit-code файл не пишется) и просто проходят через `finally` с закрытием recorder.
- rich Live-трекер не активен в pipeline-пути `cdt run` (`_tracker_start` используется только legacy-флоу), поэтому tee на уровне `sys.stdout` не может захватывать Live-кадры — изменение `cdt/ui.py` не потребовалось.
- Тест `test_direct_run_log_captures_asc_retry_diagnostics` прогоняет реальный `appstore.complete_testflight` против `urlopen`, всегда бросающего transient `URLError` (sleep замокан), и проверяет попадание retry-строк `==> ASC transient failure, attempt N/4`, итогового сообщения об исчерпании попыток и terminal summary в лог; безопасная ASC-интеграция из Validation (build 726, dry-run) неприменима к Task 4 — задача не трогает ASC-код и живые credentials недоступны. Дополнительно поведение проверено smoke-запуском реального subprocess: терминал получает сырой токен, `output.log` — `***`, failure-лог содержит summary с redaction.
- Валидация Task 4: `pytest tests/test_services_appstore.py` (49 passed), `pytest tests/test_pipeline_executor.py tests/test_pipeline_error_ux.py` (18 passed), `pytest tests/test_pipeline_resume.py tests/test_agent_first.py tests/test_redaction.py tests/test_pipeline_status_file.py` (50 passed), полный `pytest` (397 passed), `ruff check .`, `ruff format`, `python -m build`.
