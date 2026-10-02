---
worth: yes
added: 2026-10-02
---
# Support explicit conditional steps

Priority: P1. A `when`-style condition could reduce near-duplicate pipelines for input-dependent paths, but must keep the plan inspectable and artifact flow predictable. Do not replace explicit `sequence` and `parallel` branches with implicit matrix expansion or share mutable runtime values between branches.
