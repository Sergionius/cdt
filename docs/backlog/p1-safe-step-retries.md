---
worth: yes
added: 2026-10-02
---
# Make step retries and timeouts safe for side effects

Priority: P1, not a prerequisite for store publishing. App Store Connect requests have bounded retries already, but there is no general per-step policy. Design idempotency and cancellation semantics first: never automatically repeat upload, push or publication after an ambiguous failure, and do not assume a timed-out thread stopped its subprocess. Retain explicit resume for completed steps.
