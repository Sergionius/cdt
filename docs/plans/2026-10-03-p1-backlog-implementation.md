<!-- ralphex-base: 471f35ee31b83327a89c2cbffe7e13d1069d21c5 -->

# Реализация восьми направлений P1 из backlog
<!-- plan-slug: p1-backlog-implementation -->

## Goal

Реализовать согласованный минимальный объём всех восьми направлений P1: документацию существующих возможностей и iOS signing, условия по inputs, безопасные retries и поддерживаемые таймауты, изоляцию parallel values, generic webhook, локализованные тексты App Store и рецепт распространения Python-плагинов.

Это единый исполняемый план изменений, а не roadmap создания будущих планов.

## Context

- Python ≥3.10; проект использует Typer, PyYAML, pytest и Ruff.
- `cdt/pipeline/config.py` разбирает YAML v1, создаёт `ConfiguredStep` и разрешает `${inputs.*}`, `${values.*}`, env и ссылки на артефакты.
- `cdt/pipeline/planning.py` строит статический план и проверяет поток артефактов. Исполнение находится в `cdt/pipeline/executor.py`.
- `parallel` использует `ThreadPoolExecutor`, общий `PipelineContext` и дожидается остальных веток после ошибки. В `sequence` нельзя вкладывать группы.
- Регистрация артефактов защищена lock, но `ctx.values` — общий словарь. Запись статуса защищена lock, изменения коллекций перед записью — не полностью.
- Resume использует числовые step IDs, completed steps, артефакты и проверку совпадения inputs. Произвольные `values` сейчас не восстанавливаются.
- `StepMetadata` и `cdt/sdk.py` описывают плагины; загрузка происходит через явный список Python-модулей.
- `cdt/schema.py` генерирует `cdt/cdt.schema.json`; существующие тесты проверяют их соответствие.
- `hook.python_script` уже имеет `timeout`, но `subprocess.run` не обеспечивает остановку всех потомков процесса.
- ASC имеет собственные bounded retries и обработку неоднозначных мутаций. `appstore.submit_review` уже поддерживает локализованный `whats_new`.
- `notify.success` использует Telegram/Pachca и сообщает ошибки предупреждениями.
- `firebase.deploy`, `firebase.ensure_cli` и звуковые уведомления уже реализованы, но недостаточно представлены в документации.
- Существующий `docs/plans/p1-backlog-roadmap.md` координирует будущие работы и не выполняет исходный запрос на единый implementation plan.

## Scope

### Условия

Добавить расширенную запись листового шага, сохранив прежние формы:

```yaml
- step: firebase.deploy
  with: {}
  when:
    input: deploy
    equals: "yes"
```

`when` содержит имя объявленного input и ровно один оператор: `equals`, `not_equals` или `present`.

- Сравнения строковые, без интерполяции и вычисления выражений.
- `present: true` означает существующее непустое значение; `present: false` — обратное.
- Для отсутствующего input `equals` возвращает false, `not_equals` — true.
- Условия относятся только к листовым шагам, включая листья внутри существующих групп.
- Все решения вычисляются до первого шага.
- Статический план без набора inputs показывает `unknown`; план с inputs и dry-run — `run` или `skip`.
- Пропуск не считается выполнением и не создаёт артефакты.
- Условие не отменяет проверку production risk и необходимость существующего подтверждения.

### Повторы и таймауты

В расширенной записи доступны:

```yaml
retry:
  max_attempts: 3
  delay_seconds: 1
timeout_seconds: 30
```

- По умолчанию одна попытка.
- `max_attempts` — целое число от 1 до 5; задержка — конечное число от 0 до 60 секунд.
- Повтор разрешён только при `StepMetadata.retry_safe: true` и специальной ошибке `RetryableStepError`.
- Такая ошибка означает временный сбой, после которого безопасно повторить весь шаг, включая его локальные изменения.
- Обычные исключения, ошибки валидации, отмена и неоднозначные результаты мутаций не повторяются.
- Метка `risk: safe` сама по себе не разрешает retries.
- Встроенным upload/push/publication, webhook и произвольным hooks автоматические повторы не разрешаются.
- Первый рабочий путь общей retry policy — явно зарегистрированные безопасные SDK-шаги; существующие встроенные сервисные retries сохраняются.
- `timeout_seconds` передаётся в объявленный metadata параметр нативного таймаута шага. Без такой capability настройка отклоняется до исполнения.
- Первые встроенные потребители: `hook.python_script` и новый `notify.webhook`.
- Таймаут не реализуется через ожидание Python-потока и не обещает принудительной остановки произвольного Python-кода.

### Parallel values

Каждая ветка получает исходный снимок `values`; последовательные шаги одной ветки видят её изменения, соседние — нет.

После успешного завершения всех веток выполняется атомарное объединение:

- изменения разных ключей объединяются;
- одинаковые конечные значения одного ключа допустимы;
- разные значения, либо удаление против записи одного ключа, вызывают ошибку группы;
- при ошибке ветки или конфликте частичного объединения нет.

Зарегистрированные артефакты и существующая модель завершения соседних веток сохраняются. Изоляция относится к `values`, а не является полной транзакцией context, файловой системы или внешних сервисов.

### Webhook

Добавить `notify.webhook`:

- обязательные `url_env` и `payload`;
- необязательный `authorization_env`, содержащий полное значение заголовка Authorization;
- `timeout_seconds: 30`, `fail_on_error: true` по умолчанию;
- HTTPS POST с JSON, без перенаправлений и автоматических повторов;
- успешны только ответы 2xx;
- payload задаётся явно, весь context не отправляется;
- URL, авторизация и тело ответа не выводятся в сообщения и сохранённые логи.

### App Store metadata

Добавить production-шаг `appstore.update_metadata`:

- обязательная явно указанная `version`, допускающая существующую интерполяцию inputs;
- приложение выбирается по `IOS_BUNDLE_ID`;
- обязательное непустое отображение `localizations`;
- поддерживаемые поля: `description`, `keywords`, `promotional_text`, `whats_new`;
- обновляются только переданные поля существующих локализаций существующей iOS-версии;
- отсутствующее поле сохраняется; пустая строка означает явную очистку, если ASC её допускает;
- версия должна находиться в `PREPARE_FOR_SUBMISSION`;
- шаг не создаёт версию, локализацию или submission и не выбирает build;
- после неоднозначного PATCH выполняется проверка чтением, но не слепой повтор.

### Документация и плагины

Документировать существующие Firebase-шаги, звук, безопасный signing для local/CI и повторное использование устанавливаемого Python-пакета через существующие SDK и `plugins`.

Новые entry points, registry и механизм установки не вводятся.

## Out of Scope

- Screenshots и полная замена fastlane `deliver`.
- Slack-specific formatting.
- Условия по env/runtime values, Python expressions, matrix builds.
- Новая вложенность групп и отмена соседних parallel-веток.
- Изоляция каждого шага в отдельном процессе.
- Универсальная транзакция или автоматический rollback внешних эффектов.
- Новый signing manager, Ruby-зависимость, автоматическое создание сертификатов.
- Реальные публикации, сетевые проверки production-сервисов и изменения версии пакета.

## Implementation Steps

### Task 1: Добавить условия по inputs сквозным изменением

**Files:**
- Modify: `cdt/pipeline/config.py`
- Modify: `cdt/pipeline/planning.py`
- Modify: `cdt/pipeline/validation.py`
- Modify: `cdt/pipeline/preflight.py`
- Modify: `cdt/pipeline/executor.py`
- Modify: `cdt/pipeline/context.py`
- Modify: `cdt/pipeline/runner.py`
- Modify: `cdt/cli.py`
- Modify: `cdt/schema.py`
- Modify: `cdt/cdt.schema.json`
- Modify: `tests/test_pipeline_config.py`
- Modify: `tests/test_pipeline_plan.py`
- Modify: `tests/test_pipeline_executor.py`
- Modify: `tests/test_pipeline_resume.py`
- Modify: `tests/test_pipeline_status_file.py`
- Modify: `tests/test_agent_first.py`
- Modify: `tests/test_cli.py`
- Modify: `docs/pipelines.md`
- Modify: `docs/runs.md`

- [x] Добавить расширенную форму `step`/`with`/`when` без изменения старых строковых и single-key записей; неизвестные поля отклонять.
- [x] Хранить условие отдельно от опций конструктора шага; сохранить существующие step IDs, включая пропущенные листья.
- [x] Проверять объявление input, единственность оператора и тип значения; запретить динамическую интерполяцию внутри условия.
- [x] Реализовать единый evaluator условий для планирования и исполнения; вычислять таблицу решений после проверки inputs и до выполнения pipeline.
- [x] Добавить `--input` к `cdt pipeline plan`, передавать inputs в planner из dry-run; различать неизвестный набор inputs и явно переданный набор с отсутствующим optional input.
- [x] Показывать условие и решение в текстовом/JSON плане; не считать условно произведённый артефакт гарантированным при неизвестном решении.
- [x] Исключать outputs пропущенных шагов из доступного потока артефактов; для активного потребителя сохранять существующие предупреждения о недоступных артефактах.
- [x] Пропускать шаг до интерполяции его опций и создания runtime-экземпляра; записывать `skipped_steps` отдельно от `completed_steps`.
- [x] Обрабатывать группу, в которой все листья пропущены, без запуска пустого thread pool.
- [x] Сохранить проверку конфигурации и production risk для всех объявленных шагов, даже пропущенных; статический preflight без inputs остаётся консервативным.
- [x] Исправить обход листьев preflight для `sequence` внутри `parallel`, чтобы поддерживаемая форма не передавала group spec как обычный шаг.
- [x] При resume пересчитывать условия с теми же inputs; явно выбранный пропущенный шаг не выполнять. Старые статусы без новых полей принимать.
- [x] Обновить генератор и bundled schema вместе, исключив неоднозначное совпадение новой формы с plugin-схемой.
- [x] Добавить регрессии для старого YAML, всех операторов, optional inputs, групп, пропущенных опций с отсутствующим env, артефактов, production risk и resume.
- [x] Документировать синтаксис и семантику пропуска; выполнить применимые проверки из Validation.

### Task 2: Изолировать parallel values и сохранить безопасный resume

**Files:**
- Create: `cdt/pipeline/values.py`
- Modify: `cdt/pipeline/context.py`
- Modify: `cdt/pipeline/executor.py`
- Modify: `cdt/pipeline/runner.py`
- Modify: `tests/test_pipeline_context.py`
- Modify: `tests/test_pipeline_executor.py`
- Modify: `tests/test_pipeline_resume.py`
- Modify: `tests/test_pipeline_status_file.py`
- Modify: `tests/test_pipeline_error_ux.py`
- Modify: `docs/pipelines.md`
- Modify: `docs/runs.md`

- [x] Ввести mapping-совместимое хранилище `values` с корневым словарём и scoped branch-local словарями; сохранить передачу обычного dict в конструктор context и привычные операции чтения/записи.
- [x] Привязывать branch scope внутри worker через context manager; очищать scope в `finally`, включая ошибки, пропуски и повторное использование потока.
- [x] Создавать снимки всех веток до их запуска; последовательным шагам одной ветки передавать один scope, не клонируя остальные поля context.
- [x] Вычислять delta относительно исходного снимка, включая удаления; после завершения всех веток проверять конфликты и применять общий результат атомарно только при успехе.
- [x] Сообщать конфликт по step IDs и именам ключей без вывода значений; при нескольких ошибках сохранять детерминированный порядок существующего отчёта.
- [x] Синхронизировать изменение status-коллекций вместе с созданием снимка статуса; применять единый порядок захвата locks без повторного захвата нерекурсивного lock.
- [x] Добавить версионированное необязательное состояние values в status: корневые values, исходный снимок незавершённой группы и branch snapshots на границах успешно завершённых листьев.
- [x] При resume восстанавливать исходную базу группы и branch snapshots, чтобы пропуск завершённых листьев не терял их values и не делал их видимыми соседним веткам.
- [x] При частичном resume группы не объединять delta неисполненной незавершённой ветки; объяснять необходимость завершения остальных веток.
- [x] Сохранять checkpoints через существующую redaction. Если redaction изменила данные, необходимые для восстановления, отмечать снимок невосстановимым и отклонять такой resume вместо восстановления `***` или повторения завершённых side effects.
- [x] Для старых статусов сохранить прежний resume вне затронутого сценария; при необходимости восстановить отсутствующее состояние частично завершённой parallel-группы выдавать понятную ошибку до новых шагов.
- [x] Проверить изоляцию sibling branches, видимость внутри sequence, одинаковые и конфликтующие записи, удаления, ошибку ветки, skipped leaves и восстановление после частичного выполнения.
- [x] Документировать изменение прежнего общего `values`, ограничения остальных полей context и отсутствие rollback внешних эффектов; выполнить применимые проверки.

### Task 3: Реализовать opt-in retry policy для безопасных шагов

**Files:**
- Create: `cdt/pipeline/policy.py`
- Modify: `cdt/pipeline/registry.py`
- Modify: `cdt/sdk.py`
- Modify: `cdt/pipeline/config.py`
- Modify: `cdt/pipeline/validation.py`
- Modify: `cdt/pipeline/planning.py`
- Modify: `cdt/pipeline/context.py`
- Modify: `cdt/schema.py`
- Modify: `cdt/cdt.schema.json`
- Modify: `tests/test_pipeline_registry.py`
- Modify: `tests/test_pipeline_config.py`
- Modify: `tests/test_pipeline_executor.py`
- Modify: `tests/test_pipeline_status_file.py`
- Modify: `tests/test_pipeline_resume.py`
- Modify: `tests/test_agent_first.py`
- Modify: `docs/pipelines.md`
- Modify: `docs/runs.md`

- [x] Добавить `retry_safe: false` в metadata, её нормализацию, сериализацию и оба пути SDK-декоратора; экспортировать `RetryableStepError`.
- [x] Добавить `retry` в расширенную запись листа, отдельно от constructor options; валидировать границы, конечность чисел и запрет boolean вместо integer.
- [x] Разрешать `max_attempts > 1` только при явной capability; не выводить её из risk и не добавлять её автоматически встроенным шагам.
- [x] В `ConfiguredStep` применять policy одинаково для последовательных и parallel-листьев; на каждой попытке создавать новый runtime-экземпляр шага.
- [x] Повторять только `RetryableStepError`, с фиксированной задержкой и ограниченным числом попыток; остальные ошибки и `BaseException` не перехватывать как retryable.
- [x] Зафиксировать SDK-контракт: перед retryable ошибкой шаг обязан оставить context и внешние эффекты в повторяемом состоянии; executor не выполняет фиктивный общий rollback.
- [x] Отмечать лист завершённым только после успешной попытки; сохранять количество попыток и редактированную последнюю ошибку, не отмечая промежуточный сбой terminal failure.
- [x] При явном resume незавершённого шага начинать новый ограниченный цикл попыток; завершённый шаг по-прежнему пропускать.
- [x] Показывать policy и capability в inspect/plan; обновить generated schema.
- [x] Добавить SDK fixture с временной ошибкой и последующим успехом; проверить исчерпание попыток, отсутствие повторов для обычных ошибок, unsafe metadata, skipped steps и completed resume.
- [x] Проверить, что ASC, Google Play, webhook, hooks, push и публикации не получают новые автоматические повторы; документировать контракт и выполнить применимые проверки.

### Task 4: Добавить capability-based timeouts и остановку hook subprocess

**Files:**
- Modify: `cdt/pipeline/registry.py`
- Modify: `cdt/sdk.py`
- Modify: `cdt/pipeline/config.py`
- Modify: `cdt/pipeline/validation.py`
- Modify: `cdt/pipeline/planning.py`
- Modify: `cdt/pipeline/builtins.py`
- Modify: `cdt/runner.py`
- Modify: `cdt/steps/hook.py`
- Modify: `cdt/schema.py`
- Modify: `cdt/cdt.schema.json`
- Modify: `tests/test_pipeline_config.py`
- Modify: `tests/test_pipeline_registry.py`
- Modify: `tests/test_runner.py`
- Modify: `tests/test_steps_hook.py`
- Modify: `tests/test_agent_first.py`
- Modify: `docs/pipelines.md`

- [x] Добавить metadata `timeout_option: str | None`, передаваемую через SDK и inspect; capability обозначает существующий нативный параметр шага.
- [x] Поддержать положительный конечный `timeout_seconds` в расширенной записи; при отсутствии capability отклонять настройку до исполнения.
- [x] Передавать значение в объявленный constructor option; одновременную настройку envelope timeout и того же параметра в `with` отклонять как неоднозначную.
- [x] Для `hook.python_script` объявить `timeout_option: timeout`; сохранить старый синтаксис, default 30 секунд и `timeout: null` для старой формы.
- [x] Выделить в `cdt/runner.py` helper управляемого subprocess с сохранением текущего вывода hook, cwd и env; не менять все существующие command call sites.
- [x] На POSIX запускать hook в отдельной process group, при timeout отправлять TERM группе, затем KILL через ограниченный grace period и обязательно reap непосредственного процесса.
- [x] Использовать ту же очистку при прерывании ожидания; потомкам, самостоятельно покинувшим process group, не обещать гарантированную остановку.
- [x] На неподдерживаемой платформе отклонять новый envelope timeout capability, а не заявлять несуществующую гарантию дерева процессов; старое платформенное поведение отдельно документировать.
- [x] Сохранить `fail_on_error` и проверку outputs; timeout не превращать в retryable ошибку и не скрывать неуспех очистки.
- [x] Добавить контролируемые локальные тесты зависшего hook с дочерним процессом и cleanup в `finally`, а также проверки старых параметров и несовместимых настроек.
- [x] Документировать различие нативного operation timeout и жёсткого общего deadline Python-шага; обновить schema и выполнить применимые проверки.

### Task 5: Добавить безопасный generic webhook

**Files:**
- Create: `cdt/services/webhook.py`
- Create: `tests/test_services_webhook.py`
- Create: `tests/test_steps_notify.py`
- Modify: `cdt/steps/notify.py`
- Modify: `cdt/pipeline/builtins.py`
- Modify: `cdt/pipeline/preflight.py`
- Modify: `cdt/redaction.py`
- Modify: `cdt/schema.py`
- Modify: `cdt/cdt.schema.json`
- Modify: `tests/test_redaction.py`
- Modify: `tests/test_agent_first.py`
- Modify: `docs/pipelines.md`

- [x] Реализовать согласованный `notify.webhook` и регистрацию с `retry_safe: false`, `timeout_option: timeout_seconds`.
- [x] Проверять имена env keys, непустой JSON-object payload, положительный конечный timeout и boolean `fail_on_error`.
- [x] Разрешать обычную интерполяцию явно заданных payload полей, но не добавлять env, inputs, artifacts или context автоматически.
- [x] Получать destination и Authorization только по явно указанным env keys; принимать HTTPS URL с host, без userinfo и fragment.
- [x] Выполнять один POST через стандартную библиотеку с проверкой TLS и запретом redirects; не читать и не выводить тело ответа.
- [x] Считать успешным только 2xx; сетевые ошибки, timeout и другие статусы превращать в безопасную ошибку либо предупреждение согласно `fail_on_error`.
- [x] Не включать URL, Authorization, payload или сырое исключение transport в сообщения. Отражать только безопасную категорию ошибки и HTTP status при наличии.
- [x] Проверять payload на присутствие известных секретов context, destination и Authorization; отклонять такой payload до отправки, не подменяя секрет на `***` незаметно для пользователя.
- [x] На preflight проверять наличие динамически выбранных env keys без отправки запроса; plan/inspect оставлять без чтения credentials и сетевых действий.
- [x] Проверить default strict mode, warning mode, HTTP ошибки, redirects, timeout, отсутствие retries и утечек в status/output.log на mocked transport.
- [x] Сохранить Telegram/Pachca и прежнюю семантику `notify.success`; обновить schema, документацию и выполнить применимые проверки.

### Task 6: Добавить обновление локализованных текстов App Store

**Files:**
- Create: `cdt/services/appstore_metadata.py`
- Create: `tests/test_services_appstore_metadata.py`
- Modify: `cdt/steps/appstore.py`
- Modify: `cdt/pipeline/builtins.py`
- Modify: `cdt/pipeline/validation.py`
- Modify: `cdt/schema.py`
- Modify: `cdt/cdt.schema.json`
- Modify: `tests/test_steps_appstore.py`
- Modify: `tests/test_agent_first.py`
- Modify: `tests/test_pipeline_plan.py`
- Modify: `docs/pipelines.md`
- Modify: `docs/runs.md`

- [ ] Реализовать `appstore.update_metadata(version, localizations)` отдельно от `SubmitReviewStep`.
- [ ] Валидировать непустую version, непустые locale mappings, известные имена полей и строковые значения; не выполнять неявного преобразования чисел и boolean в тексты.
- [ ] Использовать существующие `_AscClient`, точный поиск приложения/версии, пагинацию и получение version localizations.
- [ ] Требовать существующую iOS-версию в `PREPARE_FOR_SUBMISSION`; не использовать get-or-create и не опираться на последнее uploaded build.
- [ ] До первой мутации проверить существование всех запрошенных локализаций и валидность всего запроса; неизвестная locale должна остановить шаг без частичного обновления.
- [ ] Сопоставить поля с ASC attributes: `description`, `keywords`, `promotionalText`, `whatsNew`; обновлять только отличающиеся явно переданные значения.
- [ ] Обрабатывать локали в стабильном порядке; PATCH выполнять с `retry_ambiguous=False`.
- [ ] После PATCH проверять результат чтением. При неоднозначном результате считать локаль успешной только при совпадении всех запрошенных полей; иначе завершаться сообщением о непроверенном результате без повторного PATCH в этом запуске.
- [ ] При обычном повторном запуске сначала читать текущее состояние и пропускать совпадающие поля; частичный успех не объявлять общей транзакцией и не откатывать автоматически.
- [ ] Сохранять безопасный итог с версией и обработанными locale names, без credentials и полного текста metadata; не заявлять отправку на review или публикацию.
- [ ] Требовать `risk: production` рекурсивно, в том числе для условно пропущенного шага; использовать существующую CLI-защиту direct/detached.
- [ ] Оставить `retry_safe: false` и не объявлять общий timeout capability: ASC сохраняет собственные настройки запросов.
- [ ] Покрыть неизвестные version/locale, неeditable state, минимальные PATCH, пустую строку, частичный успех, неоднозначный ответ, read-back mismatch и повторный запуск.
- [ ] Проверить отсутствие сетевых вызовов у inspect/plan/dry-run и отсутствие изменений в `appstore.submit_review`; обновить schema, документацию и выполнить применимые проверки.

### Task 7: Документировать существующие шаги, signing и переносимые плагины

**Files:**
- Create: `docs/ios-signing.md`
- Create: `docs/plugins.md`
- Create: `examples/reusable-plugin/pyproject.toml`
- Create: `examples/reusable-plugin/src/cdt_example_steps/__init__.py`
- Create: `examples/reusable-plugin/cdt.yaml`
- Modify: `README.md`
- Modify: `docs/pipelines.md`
- Modify: `docs/getting-started.md`
- Modify: `tests/test_examples.py`
- Modify: `tests/test_pipeline_registry.py`

- [ ] Документировать реальные параметры и назначение `firebase.ensure_cli` и `firebase.deploy`, не вводя `web.deploy`.
- [ ] Описать существующее звуковое поведение и настройки по `cdt/sounds.py`, включая ограничения среды, без обещания звука в headless CI.
- [ ] Написать signing recipe для локального Xcode/Flutter и CI: certificate с private key, provisioning profile, bundle/team matching, временный keychain, import/access и cleanup.
- [ ] Отделить code signing от ASC API authentication; не хранить секреты в YAML/repository и не предлагать вывод credentials в логи.
- [ ] Использовать существующие способы конфигурации iOS-платформ, без новых runtime-флагов, Ruby или signing manager.
- [ ] Создать минимальный пакет примера со `src` layout, `@step`, явными metadata и безопасным retryable read-only примером без реальных сетевых действий.
- [ ] Показать установку пакета в то же Python-окружение, где находится CDT, и подключение `plugins: [cdt_example_steps]` из нескольких проектов.
- [ ] Документировать доверенную природу Python imports, конфликт регистрации имён и отсутствие автоматического discovery/установки.
- [ ] Проверить пример в изолированном subprocess теста с локальным import path; не устанавливать ничего глобально и не обращаться к package index.
- [ ] Добавить ссылки из существующей документации; выполнить тесты примеров и применимые регрессии.

### Task 8: Согласовать документацию, закрыть выполненные backlog-источники и проверить интеграцию

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/plans/p1-backlog-roadmap.md`
- Modify: `tests/test_pipeline_executor.py`
- Modify: `tests/test_pipeline_resume.py`
- Modify: `tests/test_pipeline_status_file.py`
- Modify: `tests/test_agent_first.py`
- Delete: `docs/backlog/p1-document-existing-steps.md`
- Delete: `docs/backlog/p1-ios-signing-recipe.md`
- Delete: `docs/backlog/p1-conditional-steps.md`
- Delete: `docs/backlog/p1-safe-step-retries.md`
- Delete: `docs/backlog/p1-parallel-step-safety.md`
- Delete: `docs/backlog/p1-generic-notifications.md`
- Delete: `docs/backlog/p1-app-store-metadata.md`
- Delete: `docs/backlog/p1-plugin-discovery.md`

- [ ] Добавить сквозной offline-сценарий: inputs → условные листья → parallel sequence → изолированные values → безопасная повторная попытка → mocked webhook.
- [ ] Проверить отказ группы без частичного values merge, resume завершённых листьев без потери branch state и отсутствие повторной отправки завершённого webhook при `--skip-completed`.
- [ ] Проверить, что новые production metadata steps не обходят подтверждение через условие, вложенность или detached execution.
- [ ] Сверить generated schema и bundled schema, старые YAML-примеры, human-readable и JSON payloads.
- [ ] Обновить README: вместо обещания будущего demand-driven roadmap дать ссылки на реализованные возможности и их ограничения.
- [ ] Переписать старый roadmap как краткую историческую карту результатов восьми направлений с действующими ссылками на документацию, без утверждений об обязательных будущих дизайнах.
- [ ] Зафиксировать в changelog новый синтаксис и намеренное изменение parallel values, ограничения retries/timeouts и новых интеграций.
- [ ] Удалить восемь backlog-файлов только после выполнения соответствующих критериев этого плана; явно описать минимальное решение plugin discovery и отсутствие обещаний entry points.
- [ ] Не менять завершённый исторический execution plan и не переписывать его прежние решения задним числом.
- [ ] Выполнить полную Validation без реальных uploads, публикаций, credentials и production pipeline.

## Validation

После каждого Task выполнить существующие тесты изменённой области и `ruff check .`; generated schema и новые consumers должны оставаться согласованными в конце каждого Task.

Для runtime, условий, policies и parallel:

```bash
pytest tests/test_pipeline_config.py tests/test_pipeline_registry.py tests/test_pipeline_plan.py tests/test_pipeline_executor.py tests/test_pipeline_context.py tests/test_pipeline_resume.py tests/test_pipeline_status_file.py tests/test_pipeline_error_ux.py tests/test_agent_first.py tests/test_cli.py
ruff check .
```

Для subprocess:

```bash
pytest tests/test_runner.py tests/test_steps_hook.py
```

Для новых интеграций после появления соответствующих файлов:

```bash
pytest tests/test_services_webhook.py tests/test_steps_notify.py tests/test_services_notify.py tests/test_redaction.py
pytest tests/test_services_appstore_metadata.py tests/test_steps_appstore.py tests/test_services_appstore.py tests/test_services_appstore_review.py tests/test_services_appstore_state.py
```

Для документационных примеров:

```bash
pytest tests/test_examples.py tests/test_pipeline_registry.py tests/test_steps_firebase.py tests/test_sounds.py tests/test_ios_flutter.py tests/test_ios_xcode.py
```

Итоговые проверки:

```bash
pytest
ruff check .
python -m build
```

Сборка проверяет включение новых Python-модулей и bundled schema. Все HTTP/ASC обращения в тестах подменяются. Process-tree тесты используют временные локальные scripts с гарантированным cleanup и платформенным ограничением.

Дополнительно проверить существование относительных ссылок и отсутствие ссылок на удалённые backlog-файлы в актуальной документации; исторические планы не переписывать.

## Acceptance Criteria

- Все восемь направлений имеют конкретный работающий результат в согласованном минимальном объёме.
- Старые YAML-формы и pipeline без новых настроек сохраняют поведение, кроме явно документированной изоляции parallel values.
- Условия вычисляются до исполнения, видны в плане/status и не обходят production-защиту.
- Parallel-ветки не видят изменения values соседей; объединение детерминировано, конфликты не оставляют частичного результата.
- Resume не теряет необходимое branch state и не повторяет завершённые side effects автоматически.
- Retry требует явной capability и специальной ошибки; неоднозначные публикации не повторяются.
- Таймаут не выдаёт продолжающий работать поток или обычный subprocess timeout за гарантированную остановку произвольного Python-шага.
- Webhook строгий по умолчанию, не следует redirects, не повторяется автоматически и не раскрывает credentials.
- App Store metadata обновляет только четыре разрешённых текстовых поля существующих локализаций указанной версии и ничего не отправляет на review.
- Signing и reusable-plugin recipes воспроизводимы на существующих интерфейсах без нового registry или Ruby.
- Backlog закрыт по фактическому результату; текущая документация больше не подменяет реализацию roadmap будущих дизайнов.
- Полный pytest, Ruff и сборка пакета проходят.

## Execution Notes

### Task 1

- Implemented only Task 1; Tasks 2–8 and backlog files are unchanged.
- Conditions are stored separately from constructor options. Runtime decisions are frozen before execution; status records leaf skips and decisions (including all-skipped groups). Resume recomputes rather than trusts saved decisions.
- Static plans use unknown inputs; explicit plan inputs and dry-run resolve absent optional inputs. Declared artifact capabilities remain visible, but only run producers enter the guaranteed artifact flow.
- Used the repository `.venv/bin` tools (no global `python`/`pytest`/`ruff` available). Formatted changed Python files with Ruff and regenerated the bundled schema.
- Validation: the listed runtime test command passed, **260 tests**. Its initial legacy inspect JSON failure was fixed by omitting `when` for unconditional inspect nodes; the complete command was rerun successfully.
- Additional validation: `pytest` passed **1003 tests**; `ruff check .` passed; `python -m build` produced wheel and sdist with the bundled schema.
- Relative links in the changed documentation and the conditional YAML example were validated locally. No production pipelines, uploads, publication, or service credential checks were performed.

### Task 2

- Implemented only Task 2; Tasks 3–8 and backlog files are unchanged. The scoped-values implementation landed in the working tree before this run and was verified item by item here; this commit adds the remaining plan updates.
- `cdt/pipeline/values.py` provides `ScopedValues`: a `MutableMapping` over a root dict plus thread-local branch scopes. Plain dicts are still accepted by the context constructor and wrapped in `__post_init__`; all existing `ctx.values[...]`/`get`/`pop` call sites keep working.
- `ParallelStepGroup.run` snapshots the base and every branch in `begin_values_group` before submitting workers; each worker binds one scope for its whole child (including `sequence` leaves) and the scope is cleared in `finally`, so skips, failures and thread reuse cannot leak values. Other context fields are never cloned.
- After all children finish, deltas versus the base (including deletions) are merged only when no branch failed and no leaf is incomplete; conflicting writes or a deletion-versus-write conflict raise with step IDs and key names, never values. Failure or conflict leaves the root unchanged.
- Status writes keep mutating collections and serializing the snapshot under the same `RLock`; `values_state` (`version: 1`) records root values plus base and per-branch snapshots of unfinished groups, updated at each successfully completed leaf.
- Resume restores `values_state` when present; redacted checkpoints are serialized with `restorable: false` and rejected instead of restoring `***`. Legacy statuses without `values_state` resume as before; a partially completed parallel group without a checkpoint is rejected before any step runs. Partial `--resume-from` into an unfinished group never merges and names the remaining leaves.
- Regression coverage: sibling isolation and sequence visibility, identical and conflicting writes, deletions, failed branches, skipped leaves, redaction-blocked resume, one-snapshot completion/checkpoint, leaf-checkpoint resume without sibling leaks, partial resume privacy, and legacy rejection (`test_pipeline_executor.py`, `test_pipeline_context.py`, `test_pipeline_resume.py`, `test_pipeline_status_file.py`, existing `test_pipeline_error_ux.py` ordering tests).
- Docs: `docs/pipelines.md` documents the scoped values semantics and non-transactional limits; `docs/runs.md` documents `values_state` checkpoints and resume behavior.
- Validation: the listed runtime command passed, **272 tests**; `ruff check .` passed; changed Python files pass `ruff format --check`; generated/bundled schema consistency is covered by the passing `tests/test_agent_first.py` schema assertions; the `runs.md#parallel-values-checkpoints` anchor was verified. No production pipelines, uploads, publication, or service credential checks were performed.

### Task 3

- Implemented only Task 3; Tasks 4–8 and backlog files are unchanged.
- `cdt/pipeline/policy.py` defines `RetryableStepError`, the bounded `RetryPolicy` (`max_attempts` 1–5, finite `delay_seconds` 0–60), and `run_with_retry_policy`: only `RetryableStepError` triggers another attempt, with a fixed delay; other exceptions and `BaseException` propagate untouched.
- `StepMetadata.retry_safe` (default `False`, normalized to `bool`, serialized in `to_dict`) is carried through both `@step` decorator paths (kwargs and `metadata=` object) and `_normalize_metadata`; `RetryableStepError` is exported from `cdt.sdk`.
- The extended leaf record accepts `retry` next to `with`/`when`, never inside constructor options; parsing rejects unknown fields, booleans in place of integers, non-integers, non-finite and out-of-range numbers. `max_attempts > 1` is rejected by validation (`retry_requires_capability`) unless the registered step metadata declares `retry_safe: true`; `ConfiguredStep` re-checks the capability at runtime. No built-in step declares the capability (`test_builtin_steps_do_not_declare_automatic_retries`), so ASC, Google Play, webhook, hooks, push, and publication steps gained no new automatic retries; their existing service-level retry budgets are unchanged.
- `ConfiguredStep` applies the policy identically for sequential and parallel leaves, constructing a fresh runtime instance per attempt. Intermediate retryable failures are recorded via `ctx.mark_step_retry` into `step_attempts` (attempt index of the last failed attempt plus redacted error) without becoming terminal; exhaustion records the final count and then fails normally. Completed leaves are only marked after a successful attempt.
- Resume semantics: an unfinished step starts a new bounded attempt cycle (saved `step_attempts` are informational and not restored); completed leaves stay skipped under `--skip-completed`. Covered by new CLI-level resume tests.
- Inspect and plan show each leaf's `retry`; plan metadata exposes `retry_safe`. Generated and bundled schema were regenerated together with a strict `retryPolicy` definition; plugin-name disambiguation still holds (`retry` cannot collide with plugin steps).
- Regression coverage: metadata defaults/normalization/serialization and both SDK paths, `RetryableStepError` export, retry parsing bounds, capability enforcement, retry-then-success with fixed delay, fresh instance per attempt, bounded exhaustion, non-retryable and `BaseException` errors, unsafe metadata rejection, skipped leaves, sequence/parallel parity, status `step_attempts` with redaction, resume new-cycle and completed-skip (`test_pipeline_registry.py`, `test_pipeline_config.py`, `test_pipeline_executor.py`, `test_pipeline_status_file.py`, `test_pipeline_resume.py`, `test_agent_first.py`).
- Docs: `docs/pipelines.md` gains a "Step retries" section (syntax, bounds, capability, SDK contract, no generic rollback, built-in retries unchanged); `docs/runs.md` documents `step_attempts` and resume behavior; the `runs.md#step-retries` anchor was verified.
- Validation: the listed runtime command passed, **308 tests**; full `pytest` passed **1051 tests**; `ruff check .` passed; changed Python files were formatted with Ruff; bundled schema equals `schema_payload()`. No production pipelines, uploads, publication, or service credential checks were performed.

### Task 4

- Implemented only Task 4; Tasks 5–8 and backlog files are unchanged.
- `StepMetadata.timeout_option` (default `None`, non-empty string or `None`, stripped, serialized in `to_dict`) is carried through `_normalize_metadata` and both `@step` decorator paths; inspect/steps payloads and plan metadata expose it. The only built-in capability is `hook.python_script → timeout` (regression-tested so no other built-in gains it).
- The extended leaf record accepts `timeout_seconds` (positive finite number; booleans, strings, zero, negative, `.inf`, `.nan` rejected at parse time). Validation rejects the setting before execution with `timeout_requires_capability` (no capability), `ambiguous_step_timeout` (same parameter also set in `with`), and `timeout_unsupported_platform` (no POSIX process groups); `ConfiguredStep.run` re-checks all three as defense in depth, injects the value into the declared constructor option after interpolation, and shows it in plan/inspect nodes.
- `cdt/runner.py` gains `supports_process_groups()`, `run_managed_subprocess(...)`, and `_terminate_process_group(...)` with a bounded `PROCESS_GROUP_TERMINATE_GRACE_SECONDS` (5s) grace: on POSIX the child starts in its own process group, timeouts and interrupted waits (`BaseException`) send TERM to the group, then KILL after the grace period, and the direct child is always reaped; failed cleanup raises instead of being hidden. Existing `_run`/`_spawn` call sites are untouched; `hook.python_script` now runs through the managed helper while keeping its output/cwd/env behavior, `fail_on_error`, `strict_outputs`, timeout messaging, and non-retryable `typer.BadParameter` failures.
- Legacy hook syntax is preserved: single-key `timeout` stays a constructor option, default 30 seconds, `timeout: null` disables it; legacy platform behavior (only the direct child stops) is documented. `docs/pipelines.md` gains a "Step timeouts" section distinguishing native operation timeouts from the hard envelope deadline and documenting process-group guarantees and limits.
- Regression coverage: metadata normalization/serialization and both SDK paths, builtin capability ownership, envelope parsing bounds, three validation error codes, runtime injection/defense, plan/inspect exposure, schema `timeoutSeconds` definition with bundled-schema equality, scripted helper tests (exit codes, TERM→KILL→reap, failed cleanup, interrupt cleanup, platform rejection), and a real POSIX hung-hook-with-child test using marker files and guaranteed `finally` cleanup (`test_pipeline_registry.py`, `test_pipeline_config.py`, `test_runner.py`, `test_steps_hook.py`, `test_agent_first.py`).
- Validation: the listed runtime command passed, **332 tests**; the subprocess command passed, **34 tests**; full `pytest` passed **1084 tests**; `ruff check .` passed; changed files were formatted with Ruff; `python -m build` produced wheel and sdist with the regenerated bundled schema. No production pipelines, uploads, publication, or service credential checks were performed.

### Task 5

- Implemented only Task 5; Tasks 6–8 and backlog files are unchanged.
- `cdt/services/webhook.py` implements the safe delivery: `send_webhook` performs exactly one HTTPS POST of the explicitly serialized JSON payload via the standard library with a verified-TLS context and a `_NoRedirectHandler` (a 3xx surfaces as an HTTP status failure); only 2xx is success regardless of transport-specific error behaviour; the response body is never read; `WebhookError` carries only a safe category (`network_error`, `timeout`, `ssl_error`, `http_error`) plus the HTTP status when the server answered, and transport exceptions are re-raised with `from None` so the URL never enters a saved traceback.
- Option validation lives in the service and the step constructor: plain env variable names (no interpolation/whitespace/punctuation) for `url_env`/`authorization_env`, non-empty JSON-object payload, positive finite `timeout_seconds` (booleans rejected), boolean `fail_on_error`. Destination must be an HTTPS URL with a host, without userinfo or fragment; invalid ports are rejected before sending. Configuration errors always fail the step even with `fail_on_error: false`; only delivery failures (network, timeout, non-2xx) downgrade to warnings in warning mode.
- Payload fields go through the ordinary `${inputs.*}`/`${values.*}` interpolation of extended step options; nothing from env, inputs, artifacts or context is added automatically. A pre-send scan rejects the payload with a clear error (never a silent `***` substitution) when it contains the destination URL, the authorization value, or a known context secret; `SecretRedactor.find_secrets` was added to `cdt/redaction.py` as the detection helper, and the rejection message names only the matched categories.
- `NotifyWebhookStep` (`cdt/steps/notify.py`) is registered as `notify.webhook` with `retry_safe: false` and `timeout_option: timeout_seconds` (category `notify`, risk `upload`, produces `notification`); the generated and bundled schema were regenerated together (`url_env`/`payload` required). `tests/test_pipeline_registry.py::test_builtin_timeout_capability_belongs_to_hook_only` was updated to the two-capability contract (`hook.python_script` + `notify.webhook`) — a direct consequence of the planned registration.
- `cdt/pipeline/preflight.py` statically checks presence of the dynamically selected env keys (literal `url_env`/`authorization_env` names only; interpolated names are validated at run time) without sending anything; plan/inspect show option names only and perform no network actions or credential reads.
- Telegram/Pachca services and `notify.success`/`notify.prod_user_agent` semantics are untouched; `docs/pipelines.md` gains a "Generic webhook" section and lists the new built-in.
- Regression coverage: option validation, URL rules, single POST/no retries, full Authorization value, 2xx-only, safe transport categories (including timeout and TLS errors), redirect rejection, unread response body, opener TLS/no-redirect construction, payload secret rejection for destination/authorization/context secrets without echoing values, non-serializable payload, strict/warning modes, configuration errors never downgraded, metadata/no-retries/envelope-timeout validation, preflight dynamic keys, plan/inspect name-only output, and end-to-end leak checks in status/output.log on a mocked transport (`test_services_webhook.py`, `test_steps_notify.py`, `test_redaction.py`, `test_agent_first.py`).
- Validation: the new-integrations command passed, **73 tests**; the listed runtime command passed, **333 tests**; full `pytest` passed **1133 tests**; `ruff check .` passed; changed files were formatted with Ruff; bundled schema equals `schema_payload()` (asserted by the passing `tests/test_agent_first.py` schema tests). No production pipelines, uploads, publication, or service credential checks were performed.

