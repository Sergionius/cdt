<!-- ralphex-base: 69d5aa9d6f79ebc4fc8c63fffd34704725d6445b -->

# Закрытие P2: опциональные логи прямого запуска и измерение сборок
<!-- plan-slug: publish-android-app-bundle -->

## Goal

Закрыть два оставшихся P2-пункта согласованным минимальным решением:

- добавить `cdt run <pipeline> --capture-output`: неинтерактивный запуск с сохранением редактированного объединённого stdout/stderr и одновременным выводом в терминал;
- сохранять длительность build-шагов для последующего сравнения запусков;
- документировать отказ от дополнительного кэша CDT до появления измеренного узкого места и безопасной модели инвалидирования.

Обычный прямой запуск и detached execution сохраняют существующее поведение. Build-шаги продолжают запускать инструменты сборки, а не переиспользовать старые артефакты.

## Context

- Python ≥3.10; проект использует Typer, pytest, pytest-cov, Ruff и `python -m build`. CI проверяет Linux/macOS и Python 3.10–3.13.
- `cdt/cli.py:run_pipeline` проверяет inputs и production confirmation, затем вызывает `run_configured_pipeline`.
- `cdt/pipeline/runner.py` создаёт run record и устанавливает `RunOutputRecorder` для обычного прямого запуска.
- `cdt/runs.py:RunOutputRecorder` перехватывает Python stdout/stderr, но не вывод внешних процессов, унаследовавших файловые дескрипторы терминала.
- `cdt/agent_release_worker.py` уже захватывает объединённый вывод дочернего CDT, применяя incremental UTF-8 decoding и `StreamingRedactor`.
- `cdt/runner.py` в pretty-режиме перенаправляет вывод команд во временные файлы; в verbose-режиме команды наследуют stdout/stderr.
- Внешние команды запускаются не только через `ctx.runner`: Xcode вызывает `_run` напрямую, hooks используют managed subprocess. Поэтому захват только в `CommandRunner` не покрывает существующие пути.
- `cdt/redaction.py` содержит общую защиту секретов и ограниченный буфер незавершённой строки. Полнота распознавания произвольных секретов не гарантируется.
- Run records находятся в `.cdt/runs/<run-id>/`; автоматического удаления нет. `cdt logs` повторно применяет redaction при чтении.
- `cdt/pipeline/config.py:ConfiguredStep.run` — общая точка выполнения configured leaf, включая retry policy. Условия и resume пропускают листья до её вызова.
- Metadata `risk="build"` уже объявлена для Android AAB/APK, Flutter IPA, Xcode IPA, web build и Python distribution build.
- `PipelineContext` синхронизирует status-записи через `RLock`. Публичный status payload собирается отдельно в `cdt/agent_release.py`.
- В backlog остались `docs/backlog/p2-direct-run-output-capture.md` и `docs/backlog/p2-build-up-to-date-checks.md`. Оба имеют `worth: later`, а не подтверждённую необходимость реализации исходного предложения целиком.

## Scope

### Опциональный захват вывода

- Публичный флаг: `cdt run <pipeline> --capture-output`; по умолчанию выключен.
- Первый поддерживаемый вариант — POSIX: Linux/macOS. На платформе без необходимых process-group primitives флаг отклоняется до создания run record и запуска шагов.
- Захват выполняется родительским foreground supervisor вокруг дочернего процесса CDT, без PTY и без фонового отсоединения.
- Дочерний процесс получает `stdin=DEVNULL`, объединённый stdout/stderr и принудительный verbose-режим CDT. Настройка действует только на этот дочерний запуск.
- Вывод проходит redaction до записи в `output.log` и до показа пользователю. Это отличается от обычного direct run, где терминальная копия остаётся исходной.
- Гарантия относится к выводу, поступающему в stdout/stderr запуска. Она не распространяется на приватные файлы сторонних инструментов, подавленный ими вывод и процессы, намеренно перенаправившие свои дескрипторы.
- Один запуск создаёт один run record с `detached: false`. Родитель владеет capture-log и итоговым exit-файлом; дочерний pipeline сохраняет status и artifacts.
- Production confirmation, inputs, resume, IDs и пользовательский status-file сохраняют свою семантику.
- Detached execution не получает новый capture-режим и не меняет свою модель логирования.
- Retention остаётся ручным: CDT не удаляет предыдущие run records. Для capture-log устанавливаются права доступа только владельцу.
- Полнота лога ограничивается существующей защитой от чрезмерно длинных строк; безопасный маркер замены должен оставаться видимым.

### Измерение build-шагов

- Измерять каждое реально начавшееся выполнение configured leaf с metadata `risk="build"`, включая соответствующие SDK-шаги.
- Добавить необязательное поле `build_timings` в status, индексированное существующим leaf step ID.
- Запись содержит `name`, `started_at`, `finished_at`, `duration_seconds`, `outcome`.
- Начальная запись содержит имя и UTC `started_at`; до завершения остальные поля равны `null`.
- Итоговый `outcome`: `success`, `failed` или `cancelled`.
- `duration_seconds` вычисляется через monotonic clock. Это elapsed time всего вызова leaf, включая разрешение опций, retries, retry delays и регистрацию артефактов, а не CPU time или чистое время компилятора.
- При аварийном убийстве процесса незавершённая запись остаётся без итоговой длительности; данные не выдумываются.
- Условно пропущенные и завершённые resume-листья не получают новых измерений.
- Новый run record содержит только измерения текущего запуска; старые durations не восстанавливаются и не складываются.
- Parallel timings независимы; сумма длительностей веток не объявляется длительностью pipeline.

## Out of Scope

- Кэш CDT, fingerprints, up-to-date decisions, автоматический пропуск build-команд и повторная отправка старых артефактов.
- Реальные замеры на пользовательских мобильных проектах, production-сборки, uploads и сетевые публикации.
- PTY, интерактивные prompts, terminal resize forwarding и сохранение UI внешних инструментов.
- Полный контроль процессов, самостоятельно покинувших управляемые группы.
- Автоматическая ротация логов, новая retention-команда, облачное хранение логов.
- Универсальный profiler, dashboard, новая БД, новые зависимости и отдельная команда сравнения запусков.
- Изменение legacy flows вне декларативного `cdt run`.
- Изменение retry, timeout, parallel values и production-risk контрактов.

## Implementation Steps

### Task 1: Реализовать foreground-supervisor с безопасным потоковым выводом

**Files:**
- Create: `cdt/foreground_run.py`
- Create: `tests/test_foreground_run.py`
- Modify: `tests/test_redaction.py`

- [x] Реализовать узкий внутренний helper для запуска дочернего CDT с заданными argv, cwd и env: без shell, с `stdin=DEVNULL`, stdout pipe и `stderr=STDOUT`.
- [x] Запускать ребёнка в отдельной POSIX process group; передавать `-u` текущему Python для неблокирующего по буферизации Python-вывода. Не обещать снятия внутренней буферизации сторонних CLI.
- [x] Читать поток ограниченными блоками, применять incremental UTF-8 decoder с `errors="replace"` и существующий `StreamingRedactor`; записывать и показывать только отредактированные данные.
- [x] Не использовать сырые временные файлы и не накапливать полный вывод в памяти. Сохранять существующую fail-closed обработку oversized lines и завершать pending строку при EOF.
- [x] Открывать capture-log до запуска ребёнка с правами `0600`; при невозможности открыть лог завершаться до исполнения pipeline.
- [x] Явно обрабатывать ошибки чтения/записи и BrokenPipe терминального вывода: не переключаться на сырое логирование, не оставлять ребёнка без наблюдения и не сообщать об успешном захвате.
- [x] При SIGINT/SIGTERM инициировать корректное прерывание ребёнка с возможностью выполнить его cleanup; после ограниченного grace period эскалировать TERM/KILL всей оставшейся process group и reap непосредственного ребёнка.
- [x] Проверять оставшуюся группу независимо от завершения её лидера. Ограничить ожидание EOF после завершения ребёнка, чтобы унаследованный потомком pipe не мог навсегда подвесить supervisor.
- [x] Не выдавать завершение собственной группы за гарантию остановки процессов, создавших отдельную session/group. Не скрывать ошибки cleanup.
- [x] Добавить тесты stdout/stderr, UTF-8 и секретов на границах chunks, строки без newline, oversized output, ненулевого exit code, ошибок файлового ввода-вывода и отмены.
- [x] Добавить POSIX-регрессию с потомком, удерживающим pipe и игнорирующим TERM; обеспечить ограниченный timeout теста и cleanup в `finally`.

### Task 2: Подключить capture-output к CLI и единому run lifecycle

**Files:**
- Modify: `cdt/cli.py`
- Modify: `cdt/pipeline/runner.py`
- Modify: `cdt/runs.py`
- Modify: `cdt/foreground_run.py`
- Modify: `tests/test_foreground_run.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_agent_first.py`
- Modify: `tests/test_agent_release.py`
- Modify: `tests/test_pipeline_resume.py`
- Modify: `tests/test_orca_status.py`

- [x] Добавить публичный `--capture-output` и скрытый служебный признак дочернего foreground-capture запуска; не использовать наличие `--run-id` как единственный источник определения detached/direct режима.
- [x] Разрешить служебный признак только вместе с существующим run ID и без публичного `--capture-output`; не допускать рекурсивного создания supervisor и дублирования run records.
- [x] Сохранять обычную проверку конфигурации, inputs и production confirmation до реального запуска. Повторно применять защиту production в ребёнке; передавать только предоставленный пользователем `--confirm`.
- [x] Оставить `--dry-run --capture-output` неисполняющим: без subprocess, run record и изменения логов.
- [x] Передавать ребёнку pipeline, inputs, IDs, resume-флаги, resume status path и пользовательский status-file без потерь и shell-интерполяции.
- [x] Создавать единственный foreground run record с `detached: false`, командой с публичным capture-флагом и необязательным manifest-полем `capture_output: true`; не сохранять env или credentials.
- [x] Разделить внутренне владение output recorder, PID и exit-файлом от значения `detached`: обычные direct/detached callers сохраняют текущие defaults, capture-child не устанавливает `RunOutputRecorder` и не конкурирует с supervisor за эти файлы.
- [x] Передавать capture-child `CDT_UI=verbose` только через его окружение: `_run`/`_spawn` перестают скрывать внешние stdout/stderr во временных pretty-логах. Обычные режимы runner не менять.
- [x] Обеспечить live PID supervisor, единственный вывод `Run: …` и отсутствие повторных Orca lifecycle-уведомлений от ребёнка. Сохранить запрет detached stop API на остановку direct run.
- [x] После завершения ребёнка сохранять согласованные exit code и terminal status. При startup failure либо отсутствии terminal status записывать безопасную ошибку вместо ложного `success`.
- [x] При capture failure или отмене сохранять уже полученные artifacts, completed steps и checkpoints, не запускать pipeline повторно и не стирать сведения о выполненных side effects.
- [x] Применять terminal fallback к основному и пользовательскому mirror status-файлам только после прекращения конкурентных записей ребёнка.
- [x] Добавить offline end-to-end тест с временным plugin/hook, выводящим строки через Python, `os.write` и дочерний subprocess. Проверить один run record, отсутствие дублей и секретов в логе и отображаемом capture-выводе.
- [x] Проверить old direct run, detached worker, production rejection, dry-run, resume, `cdt status`, `cdt logs`, отказ на неподдерживаемой платформе и восстановление управления терминалом после отмены.

### Task 3: Добавить измерения build-листьев в status

**Files:**
- Modify: `cdt/pipeline/config.py`
- Modify: `cdt/pipeline/context.py`
- Modify: `cdt/agent_release.py`
- Modify: `tests/test_pipeline_executor.py`
- Modify: `tests/test_pipeline_status_file.py`
- Modify: `tests/test_pipeline_resume.py`
- Modify: `tests/test_agent_release.py`
- Modify: `tests/test_agent_first.py`

- [x] Добавить `build_timings` и синхронизированные методы начала/завершения измерения в `PipelineContext`; monotonic start хранить только в памяти, не сериализовать.
- [x] В `ConfiguredStep.run` измерять только листья с metadata `risk="build"` вокруг полного исполнения leaf, а не отдельно вокруг каждой retry-попытки.
- [x] Финализировать outcome в `finally`/эквивалентной защищённой структуре, не изменяя исходный результат или исключение шага; KeyboardInterrupt и прерывания отмечать как `cancelled`.
- [x] Сохранять UTC timestamps и неотрицательную конечную длительность через существующий lock и redaction status-механизм.
- [x] Не добавлять таймеры группам и не смешивать их с branch values/checkpoints. Параллельные листья должны иметь независимые записи по стабильным IDs.
- [x] Пробросить необязательное поле через `release_status`, чтобы оно было доступно в `cdt status --json`, human-readable status и `agent-release status`.
- [x] Сохранить чтение старых status-файлов без нового поля и существующую schema version для обратно совместимого добавления.
- [x] При resume не восстанавливать старые timers; пропущенные completed/conditional листья остаются без новых измерений.
- [x] Добавить детерминированные тесты с подменёнными часами: success, failure, cancellation, retry с задержкой, parallel sequence, условный skip, completed resume и старый status.
- [x] Проверить, что обычный build-step по-прежнему вызывает runner при каждом новом исполнении, даже когда выходной artifact уже существует; новая телеметрия не вводит cache hit или up-to-date shortcut.

### Task 4: Документировать контракт, зафиксировать решение по кэшу и закрыть P2

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/runs.md`
- Modify: `docs/pipelines.md`
- Create: `docs/build-performance.md`
- Modify: `tests/test_foreground_run.py`
- Modify: `tests/test_pipeline_status_file.py`
- Delete: `docs/backlog/p2-direct-run-output-capture.md`
- Delete: `docs/backlog/p2-build-up-to-date-checks.md`

- [ ] Описать различия трёх режимов: обычный direct run, foreground `--capture-output` и detached execution; показать команды запуска и чтения единого run record.
- [ ] Зафиксировать POSIX-ограничение capture-режима, отсутствие stdin/PTY, объединение stdout/stderr без обещания глобального порядка строк разных процессов и принудительный verbose transport.
- [ ] Объяснить, что capture-режим показывает редактированный поток; prompts не поддерживаются, сторонняя буферизация может задерживать вывод, скрытые/private логи не перехватываются.
- [ ] Описать redaction, права доступа, ограничение длинных строк, остаточный риск неизвестных секретов и существующую ручную retention policy. Не предлагать включать секретосодержащие debug-режимы внешних CLI.
- [ ] Документировать формат `build_timings`, незавершённые измерения, включение retries/delays и семантику нового запуска при resume.
- [ ] В `docs/build-performance.md` дать воспроизводимую инструкцию сравнения нескольких run records: одинаковые build inputs/toolchain/signing, отдельный учёт cold/warm состояния нативных кэшей и сравнение одинаковых успешных leaf names/IDs.
- [ ] Явно записать решение: CDT-level caching не реализуется без измеренной существенной стоимости и полной модели инвалидирования, охватывающей исходники, зависимости, версии инструментов, параметры сборки и signing. Старый release artifact не переиспользуется по умолчанию.
- [ ] Не приводить вымышленные benchmark results и не объявлять измерением скорости тесты с mock clocks.
- [ ] Добавить интеграционную проверку capture-запуска с build-marked SDK-шагом: редактированный вывод, terminal status и duration принадлежат одному run record; проверить также failure и resume без повторного исполнения completed leaf.
- [ ] Обновить changelog без изменения версии пакета; добавить актуальные ссылки из README и pipeline/run документации.
- [ ] Удалить оба backlog-файла только после выполнения критериев: первый закрыт opt-in функциональностью, второй — измерениями и явным решением не вводить кэш. Исторические планы не переписывать.
- [ ] Проверить относительные ссылки и отсутствие ссылок на удалённые backlog-файлы в актуальной документации; выполнить полную Validation.

## Validation

Использовать существующее виртуальное окружение; в этой рабочей копии команды доступны через `.venv/bin/`.

После Task 1:

```bash
.venv/bin/pytest tests/test_foreground_run.py tests/test_redaction.py
.venv/bin/ruff check .
```

После Task 2 дополнительно:

```bash
.venv/bin/pytest tests/test_cli.py tests/test_agent_first.py tests/test_agent_release.py tests/test_pipeline_resume.py tests/test_runner.py tests/test_steps_hook.py tests/test_orca_status.py
```

После Task 3 дополнительно:

```bash
.venv/bin/pytest tests/test_pipeline_executor.py tests/test_pipeline_status_file.py tests/test_pipeline_resume.py tests/test_agent_release.py tests/test_agent_first.py
```

Итоговые проверки:

```bash
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
```

Новые subprocess-тесты используют локальные временные scripts, ограниченные ожидания и cleanup в `finally`. Внешние build-инструменты и сервисы подменяются; реальные Flutter/Xcode-сборки и публикации не выполняются.

Проверить актуальные относительные ссылки, CLI help и совместимость JSON status payloads. Изменение YAML-схемы не требуется: новые параметры не добавляются в `cdt.yaml`.

## Acceptance Criteria

- Без `--capture-output` direct и detached execution сохраняют существующие контракты.
- На Linux/macOS capture-запуск показывает и сохраняет редактированный stdout/stderr дочернего CDT и наследующих поток внешних команд, создавая один foreground run record.
- Захват не требует ввода, не запускает pipeline повторно, не обходит production confirmation и не дублирует логи.
- Отмена и ошибки capture завершаются за ограниченное время; оставшийся потомок внутри управляемой группы не игнорируется только потому, что родитель уже вышел.
- Известные секреты, включая разделённые границей chunks, не попадают в capture-log и capture-вывод терминала.
- Для реально выполненных build-листьев доступны корректные timings и outcome; skips и resume не создают фиктивных измерений.
- Новые status-поля обратно совместимы, parallel checkpoints и retry behavior не изменены.
- Build-команды не пропускаются из-за существующих артефактов; дополнительного кэша CDT нет.
- Документация объясняет измерение повторных сборок, ограничения логирования и основание отказа от кэша.
- Оба P2-backlog файла удалены по фактическому согласованному результату.
- Полный pytest, Ruff, сборка и Twine check проходят.
