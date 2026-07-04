# Remove `cdt migrate legacy`

## Summary

Remove the legacy migration helper from CLI, runtime code, tests, documentation, and GitHub Actions. All known projects have already been migrated. README should describe only the current public interface, so legacy command mapping must be removed from README and kept only in historical notes such as `CHANGELOG.md` or archived `docs/plans/*`.

## Key Changes

### 1. CLI and runtime code

- Remove from `cdt/cli.py`:
  - `from .migration import migrate_legacy`;
  - `migrate_app = typer.Typer(no_args_is_help=True)`;
  - `migrate_legacy_command`;
  - `app.add_typer(migrate_app, name="migrate")`.
- Delete `cdt/migration.py` if no remaining imports reference it.
- Verify with `rg "migrate_legacy|legacy_pipeline_defs|cdt migrate"` that no runtime references remain.

### 2. README and docs

- Update `README.md`:
  - remove `cdt migrate legacy --dry-run`;
  - remove `cdt migrate legacy`;
  - remove the entire `Migrate legacy projects` section;
  - remove the legacy mapping table (`cdt prod` -> `cdt run prod`, etc.).
- Update `docs/pipelines.md`:
  - remove the `Migration from legacy commands` section;
  - remove migrator commands;
  - remove legacy mapping;
  - remove migrator behavior text.
- Keep historical legacy mapping only in:
  - `CHANGELOG.md`, if useful as release history;
  - archived `docs/plans/*` files.

### 3. CHANGELOG

- Add a new release note in the current/next section:
  - `Removed cdt migrate legacy after all known projects were migrated.`
- Do not reintroduce legacy mapping to README.

### 4. Tests and GitHub Actions

- Update `tests/test_cli.py`:
  - remove expectation that root help lists `migrate`;
  - remove the help-case for `migrate`;
  - optionally add a check that `cdt migrate` is no longer available.
- Update `.github/workflows/ci.yml`:
  - remove `python -m cdt.cli migrate --help`;
  - remove `python -m cdt.cli migrate legacy --help`.
- Keep smoke checks for current commands only.

### 5. Saved plan

- Save this plan as:
  - `docs/plans/2026-07-04-remove-cdt-migrate-legacy.md`.

## Test Plan

Run:

```bash
python -m ruff check .
python -m compileall cdt tests
python -m pytest -q
python -m build
python -m twine check dist/*
```

Check CLI smoke:

```bash
python -m cdt.cli --version
python -m cdt.cli --help
python -m cdt.cli run --help
python -m cdt.cli pipeline --help
python -m cdt.cli pipeline steps --help
```

Check that the removed command is unavailable:

```bash
python -m cdt.cli migrate --help
```

Expected: command not found / non-zero exit code.

## Assumptions

- Backward compatibility for `cdt migrate` is not required.
- README should contain only the current supported interface.
- `docs/plans` are historical archives, so existing old plans with legacy mapping can remain unchanged.
