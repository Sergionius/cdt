# Getting started in 5 minutes

Install CDT from a GitHub release with `pipx`:

```bash
pipx install "git+https://github.com/Sergionius/cdt.git@v0.3.3"
```

Verify the CLI:

```bash
cdt --version
cdt doctor
```

In a project that contains `cdt.yaml`, list available pipelines:

```bash
cdt pipeline list
```

Preview a test pipeline without executing steps:

```bash
cdt run test --dry-run
```

To update later:

```bash
cdt self-update --check
cdt self-update --manager pipx
```
