# P1 backlog: historical map of results

## What this document was

Until October 2026 this file coordinated eight P1 directions from `docs/backlog/p1-*.md` as a demand-driven roadmap: each direction carried its own design or demand gate and none of them was an obligation by itself. All eight directions are now implemented by the execution plan [`2026-10-03-p1-backlog-implementation.md`](2026-10-03-p1-backlog-implementation.md), and the eight backlog source files were removed after their acceptance criteria were met. This document remains as a short historical map of what was delivered; it makes no promises about future designs and introduces no new obligations.

## Results of the eight directions

| Direction | Delivered result | Documented in |
| --- | --- | --- |
| Documenting existing capabilities | `firebase.ensure_cli` and `firebase.deploy` documented as they really behave, including the explicit absence of a `web.deploy` step; terminal sound behavior and its environment limits documented | [Firebase deploy](../pipelines.md#firebase-deploy), [Terminal sounds](../pipelines.md#terminal-sounds) |
| iOS signing recipe | Repeatable local and CI signing path (certificate with private key, provisioning profile, temporary keychain, cleanup); code signing explicitly separated from ASC API authentication | [iOS code signing](../ios-signing.md) |
| Conditional steps | `when` with exactly one of `equals` / `not_equals` / `present` over a declared input; decisions are frozen before execution, visible in plan and status, and never bypass production confirmation | [Conditional steps](../pipelines.md#conditional-steps) |
| Safe step retries and timeouts | Opt-in `retry` only for steps that declare `retry_safe: true` and raise `RetryableStepError`; capability-based `timeout_seconds`; uploads, publications, webhooks and hooks gained no automatic retries | [Step retries](../pipelines.md#step-retries), [Step timeouts](../pipelines.md#step-timeouts) |
| Parallel step safety | Branch-local `values` snapshots with an atomic, conflict-checking merge; checkpointed resume that neither loses branch state nor repeats completed side effects | [Steps and parallel groups](../pipelines.md#steps-and-parallel-groups), [Parallel values checkpoints](../runs.md#parallel-values-checkpoints) |
| Generic notifications | `notify.webhook`: one HTTPS POST of an explicitly declared payload, 2xx-only success, no redirects, no automatic retries, destination and credentials never logged | [Generic webhook](../pipelines.md#generic-webhook) |
| App Store metadata | `appstore.update_metadata`: four verified text fields of existing localizations of an existing version in `PREPARE_FOR_SUBMISSION`; read-back verification, no submission on review, production confirmation required | [App Store metadata updates](../pipelines.md#app-store-metadata-updates) |
| Plugin discovery | Minimal solution on the existing SDK: ordinary Python packages imported through explicit `plugins:`; deliberately no entry points, discovery, registry, or installation machinery | [Reusable Python step plugins](../plugins.md) |

## Notes

- The plugin discovery direction was closed with the minimal option the roadmap itself named as preferable: a documented recipe for reusing installable Python packages across projects. Entry points and registry infrastructure were not added and are not promised for the future.
- The original demand-driven gates (design first, evidence before infrastructure, no speculative abstractions) shaped the delivered scope and remain visible in the documented limits of each capability above.
- New work continues through ordinary backlog and plan proposals; this document is historical and is not a roadmap of upcoming changes.
