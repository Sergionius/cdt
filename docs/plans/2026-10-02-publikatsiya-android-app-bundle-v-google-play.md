<!-- ralphex-base: cdd97a47ebdb4929452c29155a51ae79932ffb9c -->

# Публикация Android App Bundle в Google Play

## Goal

Добавить шаг `google_play.upload_aab`: загрузить конкретный AAB, оформить новый релиз в явно выбранном track и отправить недрафтовые изменения на проверку Google Play.

Поддержать ADC, release notes, staged rollout, обязательное production-подтверждение и безопасное восстановление после сетевых сбоев. Не выдавать принятие изменений Google за завершённую проверку или доступность приложения пользователям.

## Context

- `cdt/steps/android.py` регистрирует AAB как именованный `BuildArtifact` с типом `ArtifactKind.AAB`.
- Сервисы и шаги разделены между `cdt/services/` и `cdt/steps/`.
- `cdt/pipeline/builtins.py` регистрирует шаги и metadata для планирования/preflight.
- `cdt/schema.py` генерирует JSON Schema по сигнатурам шагов; `cdt/cdt.schema.json` хранит bundled-копию.
- `cdt/pipeline/validation.py` проверяет конфигурацию. Production-подтверждение в `cdt/cli.py` зависит от `risk` пайплайна.
- `cdt/pipeline/runner.py` восстанавливает inputs, артефакты и завершённые step IDs, но не промежуточное состояние внешней публикации.
- Параллельные ветки используют общий `PipelineContext`; параметры и состояние новой интеграции нельзя передавать через изменяемый `ctx.values`.
- Google API/auth клиента в зависимостях пока нет.
- Android Publisher API предоставляет edits, bundles upload/list и tracks get/update. Bundle содержит `versionCode` и SHA-256.
- `edits.commit` поддерживает `changesInReviewBehavior=ERROR_IF_IN_REVIEW`. Без этого параметра commit может отменить существующую проверку.
- Managed publishing включается и выключается в Play Console. API не предоставляет переключатель этого режима или эквивалент финальной кнопки публикации одобренных изменений.
- Проект использует pytest, Ruff, сборку Python-пакета и тест соответствия bundled/generated JSON Schema.

Источники:

- https://developers.google.com/android-publisher/api-ref/rest/v3/edits
- https://developers.google.com/android-publisher/api-ref/rest/v3/edits.bundles
- https://developers.google.com/android-publisher/api-ref/rest/v3/edits.tracks
- https://developers.google.com/android-publisher/api-ref/rest/v3/edits/commit
- https://androidpublisher.googleapis.com/$discovery/rest?version=v3

## Scope

- Один именованный AAB, один явный package name и track на вызов.
- ADC: локальные credentials, service account или настроенная CI identity.
- Новый релиз со статусом `draft`, `inProgress` или `completed`.
- Локализованные release notes, имя релиза и доля staged rollout.
- Production-подтверждение для всех track, включая internal.
- Сохраняемая операция, проверка удалённого результата и остановка при неоднозначности.
- Существующие phone/tablet tracks, включая пользовательские closed-testing tracks.
- Недрафтовые изменения отправляются стандартным API-путём на проверку; ошибки, требующие Play Console, не обходятся автоматически.

### Поведение публикации

| Сценарий | Результат CDT |
|---|---|
| `draft` | Сохраняет draft; не сообщает об отправке релиза на проверку или публикации |
| `inProgress` / `completed` | Коммитит изменения для стандартного процесса проверки/публикации Google |
| Managed publishing выключен | После одобрения Google публикация происходит по правилам Play и выбранного track |
| Managed publishing включён и применим к изменениям | После одобрения требуется ручная публикация в Play Console |
| Google требует действий в Console | CDT останавливается с понятной причиной; не меняет режим отправки сам |

CDT не определяет Managed publishing по косвенным признакам. Итоговое сообщение объясняет оба варианта, не утверждая, какой режим включён.

## Out of Scope

- Переключение Managed publishing и финальная кнопка Publish в Play Console.
- Отложенный режим «сохранить недрафтовые изменения, но не отправлять на проверку».
- Создание приложения/track, первая настройка приложения и Play App Signing.
- APK, form-factor tracks, store listing, скриншоты, управление тестировщиками.
- Продвижение между track, продолжение/завершение существующего staged rollout, замена draft.
- Отмена текущей проверки или обход требований Google.
- Автоматический выбор package, track или version code из общего runtime-контекста.
- Ожидание одобрения Google и мониторинг фактической доступности пользователям.
- App Store, общий retry framework и matrix builds.

## Implementation Steps

### Task 1: Клиент Google Play с ADC и ограниченными HTTP-операциями

**Files:**
- Modify: `pyproject.toml`
- Create: `cdt/services/google_play.py`
- Create: `tests/test_services_google_play.py`

- [x] Добавить совместимые с Python 3.10 зависимости `google-auth`, `google-api-python-client` и `filelock`. Выбрать версию клиента, поддерживающую `changesInReviewBehavior`.
- [x] Создавать Android Publisher v3 client со scope `https://www.googleapis.com/auth/androidpublisher`, без Ruby и внешнего publishing CLI.
- [x] Если итоговый `ctx.env` содержит `GOOGLE_APPLICATION_CREDENTIALS`, загружать этот ADC-файл через google-auth, разрешая относительный путь от корня проекта. Иначе использовать `google.auth.default`.
- [x] Не изменять глобальный `os.environ` ради авторизации. Создавать credentials и HTTP client отдельно для каждого вызова.
- [x] Реализовать узкие операции create/get/delete edit, list/upload bundles, get/update track, validate/commit edit.
- [x] Загружать AAB потоково через resumable upload, не читая весь файл в память; установить конечные HTTP-тайм-ауты, для upload — 120 секунд на запрос.
- [x] Отключить автоматические повторы мутаций. Для чтения разрешить ограниченные повторы временных сетевых ошибок, 429 и 5xx.
- [x] При commit всегда задавать `changesInReviewBehavior=ERROR_IF_IN_REVIEW`. Не включать `changesNotSentForReview=true` и не менять флаги после отказа Google.
- [x] Нормализовать ошибки до стадии, HTTP-кода и безопасной причины. Не выводить credential JSON, access token, Authorization header и upload-session URL.
- [x] Добавить mock-тесты ADC, refresh, тайм-аутов, ошибок доступа, commit-флагов и отсутствия слепых повторов мутаций.

### Task 2: Сохраняемая операция и безопасное восстановление

**Files:**
- Modify: `cdt/services/google_play.py`
- Modify: `tests/test_services_google_play.py`
- Create: `cdt/services/google_play_state.py`
- Create: `tests/test_services_google_play_state.py`

- [x] Хранить checkpoint в `.cdt/google-play/operations/<operation-id>.json`, отдельно от `ctx.values`. Вычислять ID из канонических параметров публикации и SHA-256 AAB.
- [x] Сохранять только несекретные данные: package, track, параметры релиза, hash, edit ID/expiry, version code, phase, исходный track и подтверждённый результат. Не сохранять credentials или upload-session URL.
- [x] Использовать версионированный формат и атомарную запись. При повреждении, неизвестной версии или ошибке записи останавливать публикацию.
- [x] Блокировать одновременные операции одного package в этом checkout через `FileLock`. Занятая блокировка возвращает ошибку; разные приложения не блокируют друг друга.
- [x] Под блокировкой обнаруживать другие незавершённые операции того же package и запрещать новую публикацию с изменёнными параметрами.
- [x] Перед внешней мутацией сохранять intent, после подтверждения — результат. Не выполнять внешние изменения, если checkpoint не удалось сохранить.
- [x] Проверять track до загрузки. Поддержать пустой track или один обычный завершённый релиз; при draft, `inProgress`, `halted`, неизвестном или сложном состоянии остановиться без замены существующего релиза.
- [x] Формировать обновление только целевого track: новый draft сохраняет действующий completed-релиз, новый staged rollout сохраняет прежний completed-релиз как базовый, новый completed-релиз заменяет его целевым новым релизом. Не добавлять старые version codes в новый релиз автоматически.
- [x] Сверять hash ответа upload с локальным AAB и использовать возвращённый Google version code, не `ctx.new_version`.
- [x] При потере ответа upload искать bundle по точному SHA-256 в сохранённом edit. Продолжать только при единственном совпадении; иначе остановиться без повторной загрузки.
- [x] При потере ответа track update сверять состояние с ожидаемым payload. Не перезаписывать конфликтующее состояние.
- [x] После подтверждённого commit сохранять успех. При потере ответа проверять edit и состояние приложения через отдельный проверочный edit; проверочный edit не коммитить.
- [x] При восстановлении признавать успех только при совпадении bundle hash/version code и ожидаемых параметров релиза. Исчезновение edit и ошибка duplicate version code сами по себе не доказывают успех.
- [x] При невозможности установить результат возвращать явную ошибку «результат неизвестен» и инструкцию проверки Play Console, не повторяя upload/commit вслепую.
- [x] Для уже подтверждённой идентичной операции возвращать сохранённый результат без мутаций.
- [x] Покрыть тестами потерянные ответы, истёкший edit, mismatched hash, повреждение checkpoint, изменение параметров, конфликт track, конкуренцию и crash между удалённым успехом и локальной записью.

### Task 3: Шаг пайплайна, production-защита и JSON Schema

**Files:**
- Create: `cdt/steps/google_play.py`
- Modify: `cdt/pipeline/builtins.py`
- Modify: `cdt/pipeline/validation.py`
- Modify: `cdt/pipeline/preflight.py`
- Modify: `cdt/cdt.schema.json`
- Create: `tests/test_steps_google_play.py`
- Modify: `tests/test_pipeline_config.py`
- Modify: `tests/test_pipeline_registry.py`
- Modify: `tests/test_agent_first.py`

- [ ] Добавить `google_play.upload_aab` с обязательными `artifact`, `package_name`, `track`, `release_status`; необязательными `release_notes: dict[str, str]`, `release_name`, `user_fraction`.
- [ ] Не задавать defaults для track и release status. Проверять package/track, существование файла, тип AAB и непустые локализованные notes при их наличии.
- [ ] Разрешить только `draft`, `inProgress`, `completed`. Для `inProgress` требовать `0 < user_fraction < 1`; для остальных статусов запрещать fraction.
- [ ] Валидировать числовые строки после input-интерполяции; отвергать bool, NaN, infinity и нечисловые значения.
- [ ] Использовать существующую интерполяцию inputs. Не выводить параметры из Firebase credentials, имени pipeline или общих mutable values.
- [ ] Рекурсивно требовать `risk: production` в `validate_pipeline` для любого Google Play шага, включая шаги внутри `sequence`/`parallel`. Динамический track не обходит защиту.
- [ ] Использовать существующее точное подтверждение direct/detached CLI, без отдельного prompt внутри шага.
- [ ] Зарегистрировать metadata: вход `android_aab`, результат `upload_result`, риск `upload`. Не объявлять `GOOGLE_APPLICATION_CREDENTIALS` обязательной переменной.
- [ ] В preflight проверять явно заданный ADC-файл без внешних мутаций. Отсутствие этой переменной не считать ошибкой; наличие credentials не выдавать за доказательство Play Console permissions.
- [ ] Dry-run, plan, inspect и schema не должны обращаться к ADC/network или создавать checkpoint.
- [ ] Выводить package, track, version code, запрошенный статус и подтверждение commit. Для draft явно писать, что создан draft; для недрафтового релиза — что изменения приняты, но одобрение/доступность не проверялись.
- [ ] Для недрафтового результата сообщать: при применимом Managed publishing выпуск требует ручного Publish; иначе дальнейший выпуск выполняет Google. Не утверждать, что CDT определил настройку.
- [ ] Перегенерировать bundled schema существующим генератором и проверить совпадение с `schema_payload()`.
- [ ] Добавить тесты контракта шага, обязательных options, production validation и безопасных итоговых сообщений.

### Task 4: Проверка CLI, resume и границ публикации

**Files:**
- Modify: `tests/test_agent_first.py`
- Modify: `tests/test_pipeline_resume.py`
- Modify: `tests/test_steps_google_play.py`
- Modify: `tests/test_services_google_play_state.py`
- Modify: `tests/test_redaction.py`

- [ ] Проверить direct/detached запуск: отсутствие или неправильное подтверждение не вызывает publishing API; точное подтверждение разрешает шаг.
- [ ] Проверить отказ `standard` pipeline до выполнения предшествующих шагов.
- [ ] Проверить resume: завершённый шаг пропускается, незавершённый использует checkpoint и сохранённый артефакт.
- [ ] Проверить, что обычный повторный запуск без resume не обходит незавершённую операцию.
- [ ] Проверить изменение package, track, notes, fraction и содержимого AAB между попытками.
- [ ] Проверить изоляцию параметров разных приложений в параллельных ветках и запрет одновременных публикаций одного package.
- [ ] Проверить draft, completed и staged rollout, включая сохранение предыдущего completed-релиза там, где он нужен.
- [ ] Проверить отказ при текущем review без отмены проверки и без повторного commit с `changesNotSentForReview=true`.
- [ ] Проверить ошибки Google, требующие Console: CDT не превращает их в успех и не обещает отправку на review.
- [ ] Проверить, что сообщения не смешивают API commit, review approval, Managed publishing и фактический выпуск пользователям.
- [ ] Проверить отсутствие секретов в диагностике, checkpoint, status и сохранённых логах. Все сценарии выполнять на fake API и временных файлах.

### Task 5: Документация и закрытие backlog-пункта

**Files:**
- Modify: `README.md`
- Modify: `docs/pipelines.md`
- Modify: `docs/runs.md`
- Modify: `CHANGELOG.md`
- Delete: `docs/backlog/p0-google-play-upload.md`

- [ ] Описать ADC, API enablement и Google Play permissions отдельно от Firebase; указать необходимость существующего приложения/track и первоначальной настройки в Console.
- [ ] Добавить production-пайплайны для internal, draft, production completed и production staged rollout с явными параметрами и точным подтверждением.
- [ ] Объяснить различия draft, отправки изменений на проверку, одобрения и фактического выпуска.
- [ ] Явно документировать: Managed publishing переключается вручную, финальный Publish при применимом управляемом режиме также выполняется вручную; CDT эти действия не автоматизирует и не определяет режим косвенно.
- [ ] Описать отказ при незавершённом релизе, текущей проверке и необходимости действий в Play Console.
- [ ] Описать checkpoint, безопасный resume и действия при неопределённом результате. Предупредить, что удаление checkpoint не является безопасным способом повторить публикацию.
- [ ] Указать, что локальная блокировка не координирует другие машины и Play Console; внешние конфликты приводят к остановке.
- [ ] Добавить Unreleased-запись и удалить backlog-файл после завершения функции.

## Validation

Выполнять применимые к текущей задаче проверки:

```bash
pytest tests/test_services_google_play.py tests/test_services_google_play_state.py
pytest tests/test_steps_google_play.py tests/test_pipeline_config.py tests/test_pipeline_registry.py
pytest tests/test_agent_first.py tests/test_pipeline_resume.py tests/test_redaction.py
ruff check .
pytest
python -m build
```

Сборка пакета нужна из-за изменения зависимостей и bundled schema. Тесты не используют реальные credentials, сеть или публикацию в Google Play.

## Acceptance Criteria

- Новый шаг принимает явные artifact, package, track и release status и работает через ADC.
- Любой Google Play шаг требует `risk: production` и точное CLI-подтверждение.
- CDT поддерживает новый draft, полный rollout и staged rollout с валидируемыми notes/fraction.
- CDT не заменяет незавершённые релизы, не отменяет review и не продвигает сборки между track.
- Недрафтовые изменения отправляются стандартным API-путём без автоматического перехода в режим «не отправлять на review».
- Managed publishing и ручная финальная публикация явно обозначены как действия Play Console.
- Успешный commit не выдаётся за завершённое review или доступность пользователям.
- Resume и повторный запуск не повторяют неопределённые мутации вслепую.
- Параллельные ветки не смешивают параметры и артефакты.
- Планирование не имеет сетевых побочных эффектов; секреты не сохраняются в checkpoint и диагностике.
- Документация, schema и автоматические проверки соответствуют реализованному поведению.
