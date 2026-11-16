# Synthetic offline reports

These JSON files are hand-authored **synthetic fixtures**, not reports captured
from a browser, real website, production CI system, or user test suite. No
production data or secrets are present. `_fixture` is extra synthetic provenance
metadata; it is retained in the original report bytes but not interpreted.

* `playwright.json`: shape checked against Playwright v1.56.1 JSON serializer and
  types. File → cart → purchase nesting; checkout exists in Chromium and Firefox;
  Chromium fails then passes; payment fails then times out; expected failure,
  unexpected pass, skipped, interrupted, and not-executed cases are distinct.
* `ctrf.json`: comprehensive reporter v0.0.29 field spellings/string-suite dialect.
  A deliberately aggregate-only record omits optional `retryAttempts`; another
  includes prior-attempt detail. The summary counts raw failures including an
  expected failure whose test-level CTRF status is passed, matching this version's
  serializer. One project only; import with `--project chromium`.

The aggregate-only variant exercises the common CTRF missing-detail case; the
inspected 0.0.29 reporter normally emits `retryAttempts` for actual retries.

Fixture expected totals after importing each once: 2 runs, 14 test observations,
16 available attempts, 4 retried observations, 1 native fail-then-pass,
1 CTRF mapped fail-then-pass, 2 terminal failures, 2 expected failures,
2 interrupted final results, 2 skipped final results, 1 unexecuted native test,
116 ms available attempt durations. These are artificial values, not benchmarks.
