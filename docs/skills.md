# CDT Agent Skills

CDT includes reusable Agent Skills under `skills/`.

## `cdt-release`

Path:

```text
skills/cdt-release/SKILL.md
```

Use this skill for agent-assisted CDT releases, including CDT's own releases through the repository's production `release` pipeline (`cdt run release --input version=X.Y.Z --confirm release`). It tells the agent to validate and inspect pipelines before execution, require exact confirmation for production after the preflight, pass the release version explicitly, use isolated run IDs for long work, wait on compact status JSON until the terminal result (push alone is not a released version), and summarize results without pasting complete build logs.

Related repository guidance:

```text
AGENTS.md
.agents/rules/cdt-release.md
```

Clients that support the `SKILL.md` directory layout can copy or symlink `skills/cdt-release/` into their skill directory. Repository-level agents should start from `AGENTS.md` and follow the linked release safety rule.

After installation, requests such as “отправь тестовую сборку через cdt” should load `cdt-release` when the client supports skill discovery.

## Human operation remains supported

The skill is optional. A developer can always run the same pipeline directly:

```bash
cdt pipeline plan test
cdt run test
```

Agent and human execution use the same pipeline configuration, safety declarations, artifact model, and run status implementation.
