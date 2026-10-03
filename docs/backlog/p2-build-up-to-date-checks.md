---
worth: later
added: 2026-10-02
---
# Evaluate build up-to-date checks only after measuring a bottleneck

Priority: P2 candidate. Flutter, Gradle and Xcode already manage their own caches; skipping a release build based on source files alone can ship stale artifacts when toolchain, signing or build parameters change. Measure repeated build cost and define a complete cache key before deciding whether CDT-level caching has value. Never reuse an old release artifact by default.
