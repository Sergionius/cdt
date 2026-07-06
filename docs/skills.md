# CDT Agent Skills

CDT includes reusable Agent Skills under `skills/`.

## `cdt-release`

Path: `skills/cdt-release/SKILL.md`

Use this skill for AI-agent assisted CDT releases. It tells the agent to validate
and inspect pipelines before execution, avoid production unless explicitly
requested and confirmed, write long command output to
`.cdt/agent-release-<pipeline>.log`, and summarize release results concisely.

Related agent rule file:

```text
.agents/rules/cdt-release.md
```

The rule file contains the short hard requirements for agents that support
repository-level rules. The skill remains the full operating procedure.

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

## Hermes setup

Install or expose this repository's `skills/` directory to Hermes, then load the
release skill in a session with:

```text
/skill cdt-release
```

## Codex and other Agent Skills clients

Copy or symlink the skill directory into the client's skills location:

```sh
mkdir -p ~/.codex/skills
ln -s /path/to/cdt/skills/cdt-release ~/.codex/skills/cdt-release
```

After installation, requests like “отправь тестовую сборку через cdt” should load
`cdt-release` automatically when the client supports skill discovery.

## Claude Code and repository-level agents

Claude Code and other agents that read repository instructions should start from
`AGENTS.md`. It points release work to `skills/cdt-release/SKILL.md` and the
hard safety rules in `.agents/rules/cdt-release.md`.
