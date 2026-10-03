# Build performance: comparing repeated builds

CDT records how long each executed build step took, so repeated builds can be
compared with real data instead of impressions. CDT only measures: it never
skips a build, reuses an artifact, or makes decisions from these numbers.

Measurements live in the `build_timings` field of the run record's
`status.json`, indexed by leaf step ID. See
[Build step timing](pipelines.md#build-step-timing) for the field format and
[Run records](runs.md) for the record layout.

## Comparing two or more runs

A comparison is only meaningful when everything except the measured build cost
stays the same.

1. **Keep build inputs identical.** Run the same pipeline with the same
   `--input` values, from the same application-project commit, with an
   unchanged pipeline definition. Step IDs are positional: changing the
   pipeline changes the IDs you would compare.
2. **Keep the toolchain and signing identical.** Record the Flutter, Xcode,
   Gradle, Java, and Node versions and the signing configuration before
   measuring. A toolchain or signing change is itself a build-cost change, not
   noise.
3. **Account for cold and warm native caches separately.** Gradle build caches,
   Xcode DerivedData, and the Flutter/Dart pub caches make a first build much
   slower than the next one. Treat a build after cleaning these caches as a
   cold measurement and subsequent builds as warm ones. Compare cold with cold
   and warm with warm; never mix or average them. CDT does not manage these
   caches and cannot tell a cold from a warm build — note it yourself per run.
4. **Compare the same successful leaves.** Match entries by leaf step ID (and
   step name) across run records, and compare only entries with
   `outcome: "success"`. A `failed` or `cancelled` entry measures an
   interruption, not build cost, and an unfinished entry (for example after a
   killed process) has no duration at all.
5. **Read the measurements from the run records:**

   ```bash
   cdt run android --input flavor=prod          # each run gets its own record
   cdt status                                   # newest record, includes build_timings
   jq '.build_timings' .cdt/runs/<run-id>/status.json
   cdt status --json | jq '.build_timings'
   ```

   Compare per-leaf `duration_seconds` between the selected records. Duration
   covers the whole leaf call, including option resolution, retries, and retry
   delays — a leaf that retried is not comparable with one that did not. In
   parallel groups each leaf is timed independently, and the sum of branch
   durations is not the pipeline duration.

Each run record contains only its own run's measurements. Old durations are
never restored or added up, so a resume measures only what actually executed in
the new run.

## Why CDT has no build cache

CDT-level caching — fingerprints, up-to-date decisions, skipping build
commands, or resending a previous release artifact — is deliberately **not
implemented**. This is a decision, not a missing feature:

- Flutter, Gradle, and Xcode already manage their own caches; they invalidate
  on their own inputs far better than a source-file fingerprint would.
- Skipping a release build based on source files alone can ship a stale
  artifact when the toolchain, build parameters, entitlements, or signing
  configuration changed without touching a source file.
- A safe cache key must cover sources, dependencies, tool versions, build
  parameters, and signing — a complete invalidation model that does not exist
  today.

CDT will reconsider only if measurements from the instructions above show a
substantial, repeated cost that the native caches do not already absorb, and a
complete invalidation model can be defined and reviewed. Until then, every run
builds fresh, and an old release artifact is never reused by default.

## Honest measurement

- This documentation contains no benchmark numbers. Build durations depend on
  the application project, the toolchain, and the hardware; collect your own
  with the instructions above instead of assuming any published figure.
- CDT's automated tests use substituted (fake) clocks to verify measurement
  correctness — that entries appear, finalize with the right outcome, and stay
  unfinished after cancellation. They are correctness tests, not speed
  measurements, and must never be quoted as performance results.
