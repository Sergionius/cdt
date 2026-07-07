# Release resume, runtime status, and preflight plan

## Goal

Improve CDT for long mobile release pipelines by making failed runs easier to resume, making parallel execution observable for agents/CI, and adding stricter preflight checks before upload/release steps run.

## Scope

Implement in `feature/release-resume-status`:

1. `cdt run --resume-from <step>` and `--skip-completed`.
2. Richer runtime status for parallel groups.
3. `cdt pipeline validate --strict`.
4. Metadata-driven `requires_env` plus `cdt pipeline preflight <pipeline>` for required env keys and external tools.

Out of scope for this branch: artifact manifest, agent-release log tail command, and generic step timeout/retry policy.

## Design

Resume uses the existing run status JSON as the source of truth. Previous `completed_steps`, version fields, and artifacts are restored into `PipelineContext`. CDT validates restored artifact paths exist before allowing a resumed run, because upload steps often depend on build outputs from the prior run. `--resume-from` skips all earlier steps until a matching step name is reached. `--skip-completed` skips steps already listed in the previous status file. Both options are explicit so normal runs keep existing behavior.

Parallel status keeps backward compatibility with `current_step: parallel`, but adds child-level fields: `running_steps`, `parallel_completed`, and `parallel_failed`. This lets agents and CI see which iOS/Android branch is active or failed without reading full logs.

Strict validation promotes planner warnings to command failures. The warnings remain visible in JSON output and text output, but `--strict` exits non-zero when artifact-flow or custom-metadata warnings are present.

Preflight extends `StepMetadata` with `requires_env`. `cdt pipeline preflight <pipeline>` collects metadata for only the selected pipeline, checks external tools with `shutil.which`, checks required env key presence by name only, and never prints secret values.

## Testing

Add tests for resume skipping, restored artifact validation, parallel status fields, strict validation failure, metadata serialization, and preflight env/tool checks. Run focused pytest files during implementation, then `pytest` and `ruff check .`.
