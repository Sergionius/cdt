# Security policy

## Reporting a vulnerability

Report vulnerabilities privately through GitHub Security Advisories:

https://github.com/Sergionius/cdt/security/advisories/new

Do not open a public issue for an unpatched vulnerability. Include the affected CDT version, operating system, reproduction steps, impact, and any suggested mitigation. Avoid including real credentials, signing keys, application artifacts, or customer data.

## Security model

CDT executes trusted project-local release automation with the permissions of the current user. A `cdt.yaml` file, imported plugin, Python hook, build tool, or external CLI may run commands, access credentials, modify files, upload artifacts, or push Git refs.

Before execution:

- review `cdt.yaml` and every configured plugin;
- use `cdt pipeline plan`, `validate`, and `preflight`;
- keep secrets in the environment or platform credential stores;
- never commit signing keys or tokens;
- mark production pipelines with `risk: production`;
- require exact human confirmation for production work.

Run manifests and status files intentionally omit environment variable values. Build logs may still contain output from third-party tools, so treat `.cdt/runs/` as potentially sensitive and keep it out of version control.

## Supported versions

Security fixes are provided for the latest released version. Upgrade before reporting an issue already fixed in a newer release.
