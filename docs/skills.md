# CDT Agent Skills

CDT includes reusable Agent Skills under `skills/`.

## `cdt-release`

Path: `skills/cdt-release/SKILL.md`

Use this skill for AI-agent assisted CDT releases. It tells the agent to validate
and inspect pipelines before execution, avoid production unless explicitly
requested, write long command output to `.cdt/agent-release-<pipeline>.log`, and
summarize release results concisely.

## Pi setup

Install the CDT repository as a Pi package:

```sh
pi install git:github.com/Sergionius/cdt
```

Or install from a local checkout:

```sh
pi install /path/to/cdt
```

Pi discovers the conventional `skills/` directory automatically.

## Codex and other Agent Skills clients

Copy or symlink the skill directory into the client's skills location:

```sh
mkdir -p ~/.codex/skills
ln -s /path/to/cdt/skills/cdt-release ~/.codex/skills/cdt-release
```

After installation, requests like “отправь тестовую сборку через cdt” should load
`cdt-release` automatically when the client supports skill discovery.
