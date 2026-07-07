# StepMetadata artifact contract cleanup

## Summary

Сделать breaking cleanup artifact metadata contract: заменить неясные `requires_artifacts`/`produces` на явную универсальную схему требований и production metadata.

Для обычных пользователей `cdt.yaml` не меняется. Breaking change касается SDK/plugin authors и потребителей `cdt pipeline plan --json`.

## Goals

- Уточнить semantics artifact dependencies: поддержать `all`/`any` без неоднозначности.
- Убрать магический `_ARTIFACT_NAME_PRODUCING_TYPES` whitelist.
- Сделать metadata самодостаточной: из неё должно быть понятно, какие YAML options дают artifact names.
- Сохранить runtime behavior пайплайнов и текущий `cdt.yaml` синтаксис.

## Non-goals

- Не менять формат `cdt.yaml` для обычных пользователей.
- Не менять выполнение step'ов.
- Не добавлять blocking validation вместо текущих warning'ов.
- Не менять packaging rules для `plans/`.

## Proposed API

Добавить structured metadata dataclass'ы в `cdt/pipeline/registry.py`:

```python
@dataclass(frozen=True)
class ResultRequirement:
    result_types: tuple[str, ...]
    mode: str = "all"  # "all" | "any"
    name_options: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResultProduction:
    result_type: str
    name_options: tuple[str, ...] = ()
```

Изменить `StepMetadata`:

```python
@dataclass(frozen=True)
class StepMetadata:
    name: str
    description: str = ""
    category: str = "custom"
    risk: str = "custom"
    requires: tuple[ResultRequirement, ...] = ()
    produces: tuple[ResultProduction, ...] = ()
    external_tools: tuple[str, ...] = ()
    plugin: bool = False
```

Это breaking change: старое `requires_artifacts` удаляется, а `produces` перестаёт быть `tuple[str, ...]`.

## Built-in metadata updates

- `ios.flutter_build_ipa`, `ios.xcode_build_ipa`:
  - `produces=(ResultProduction("ios_ipa", name_options=("artifact",)),)`
- `android.build_aab`:
  - `produces=(ResultProduction("android_aab", name_options=("artifact",)),)`
- `android.build_apk`:
  - `produces=(ResultProduction("android_apk", name_options=("artifact",)),)`
- `web.build`:
  - `produces=(ResultProduction("web_build"),)` because the current step does not expose a configured artifact name option.
- `appstore.upload_testflight`:
  - `requires=(ResultRequirement(("ios_ipa",), name_options=("artifact",)),)`
  - `produces=(ResultProduction("upload_result"),)`
- `firebase.upload_app_distribution`:
  - `requires=(ResultRequirement(("android_aab", "android_apk"), mode="any", name_options=("artifact",)),)`
  - `produces=(ResultProduction("upload_result"),)`
- `artifact.copy_to_downloads`:
  - `requires=(ResultRequirement(("artifact",), name_options=("artifact",)),)`
  - `produces=(ResultProduction("file"),)`
- `web.copy`:
  - if modeled as consuming web build: `requires=(ResultRequirement(("web_build",),),)`
  - `produces=(ResultProduction("file"),)`
- `notify.success`:
  - `produces=(ResultProduction("notification"),)`
- `tracker.comment`:
  - `produces=(ResultProduction("tracker_comment"),)`

## Planner changes

- Remove `_ARTIFACT_NAME_PRODUCING_TYPES`.
- Infer `artifact_flow.requires_names` from `ResultRequirement.name_options`.
- Infer `artifact_flow.produces_names` from `ResultProduction.name_options`.
- Derive `requires_types` from structured requirement groups.
- Derive `produces_types` from structured productions.
- Preserve current warning behavior:
  - warnings only, no blocking errors;
  - sequential dependencies allowed;
  - parallel sibling dependency warning if one branch consumes artifact produced by another branch.

## JSON plan impact

`cdt pipeline plan --json` metadata becomes structured.

Example metadata shape:

```json
{
  "description": "Upload an app artifact to Firebase App Distribution.",
  "requires": [
    {
      "result_types": ["android_aab", "android_apk"],
      "mode": "any",
      "name_options": ["artifact"]
    }
  ],
  "produces": [
    {
      "result_type": "upload_result",
      "name_options": []
    }
  ],
  "external_tools": ["firebase"],
  "plugin": false
}
```

`artifact_flow` may keep compact fields for planner consumers:

```json
{
  "requires_names": ["app_aab"],
  "produces_names": [],
  "requires_types": ["android_aab", "android_apk"],
  "produces_types": ["upload_result"]
}
```

If useful, add requirement group details later, but do not overcomplicate unless tests need it.

## User impact

For ordinary `cdt.yaml` users:

- no YAML syntax changes;
- no step option changes;
- no runtime behavior changes;
- no migration needed.

Affected users:

- SDK/plugin authors using `StepMetadata(requires_artifacts=..., produces=("...",))`;
- CI/tools parsing `cdt pipeline plan --json` metadata.

Document this as a breaking SDK/JSON contract change.

## Docs updates

- Update `docs/pipelines.md`:
  - explain `ResultRequirement` groups;
  - explain `mode="all"` vs `mode="any"`;
  - explain `name_options` and static artifact-name inference.
- Update README if it mentions metadata or JSON plan shape.
- Update `CHANGELOG.md` with breaking note.

## Test plan

Update existing tests and add focused coverage for:

- `StepMetadata.to_dict()` structured `requires`/`produces`.
- SDK decorator accepts new metadata classes.
- Built-in metadata:
  - iOS/Android build steps produce typed artifacts with `artifact` name option;
  - `firebase.upload_app_distribution` uses `mode="any"` for AAB/APK;
  - `artifact.copy_to_downloads` consumes configured artifact name.
- Planner:
  - artifact name inference comes from `name_options`, not whitelist;
  - sequential artifact flow warnings still work;
  - parallel sibling dependency warning still works;
  - plan JSON remains clean and does not duplicate `name`/`category`/`risk` inside compact metadata.

Run before completion:

```bash
pytest
ruff check .
python -m build
```
