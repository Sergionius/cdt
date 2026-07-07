# Remove `env` alias from build steps

## Summary

Make the YAML API unambiguous: Android and iOS Flutter build steps should accept only `profile` for CDT build profiles. The old `env -> profile` alias must be removed to avoid confusing build profiles with process/project environment variables.

## Key Changes

### 1. Android build steps

- Update `cdt/steps/android.py`:
  - remove the `env` parameter from `_AndroidBuildBase.__init__`;
  - replace `self.profile = env or profile` with `self.profile = profile`.

### 2. iOS Flutter build step

- Update `cdt/steps/ios.py`:
  - remove the `env` parameter from `IosFlutterBuildIpaStep.__init__`;
  - replace `self.profile = env or profile` with `self.profile = profile`.

### 3. Validation

- Add validation for unknown step options in configured pipeline steps.
- `env` in `android.build_aab`, `android.build_apk`, or `ios.flutter_build_ipa` should produce a clear validation error.
- Preferred hint: `Use 'profile' instead.`

### 4. Docs/examples

- Check README, docs, examples, and tests for build-step `env` usage.
- Keep `env` only where it means environment variables, for example hook env mappings.

## Test Plan

Run:

```bash
python -m ruff check .
python -m compileall cdt tests
python -m pytest -q
```

Add/adjust tests for:

- `profile` still works for Android/iOS Flutter build steps;
- `env` is rejected as an unknown build-step option with a useful hint.

## Assumptions

- Backward compatibility for the `env` alias is no longer required.
- Public `cdt.yaml` v1 build profile option is `profile` only.
