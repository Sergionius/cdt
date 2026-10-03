---
worth: later
added: 2026-10-02
---
# Decide whether direct runs need complete external CLI logs

Priority: P2. `docs/runs.md` says direct `cdt run` saves CDT-owned diagnostics but may not capture raw third-party subprocess output. Humans see that output in the terminal, and detached runs already capture combined output; storing more raw data can expose secrets. Reconsider only if real post-run debugging is blocked, with a reviewed redaction and retention model.
