---
worth: later
added: 2026-10-02
---
# Revisit parallel branch safety and schema limits

Priority: P1 candidate. `docs/pipelines.md` documents that sibling branches continue after failure, nested groups are limited, and `ctx.values` writes from parallel branches are not guaranteed thread-safe. Establish a concrete affected pipeline before changing cancellation or nesting semantics; preserve explicit branch-local parameters and artifact boundaries rather than adding matrix builds.
