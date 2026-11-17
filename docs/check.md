# qawrench check

`qawrench check` reads the folder your browser-test CI job uploads and tells you
**which parts of the run you should not trust**: tests that only passed on
retry, HAR recordings that replay ambiguously, traces with missing frames,
pytest tests that leave state behind for the next test, accessibility
violations, Lighthouse numbers that are mostly noise, and broken links with
the line they are on.

It is the front door to the other qawrench commands. It does not reimplement
their analysis: each check calls the read-only Python API of one tool and turns
the result into one report. It never writes a file, never runs a test or a
browser, and never opens a network connection.

```sh
uvx qawrench check test-results/                        # or: pipx install 'qawrench[all]'
qawrench check test-results/ playwright-report/ lychee.json
qawrench check . --format markdown >> "$GITHUB_STEP_SUMMARY"
qawrench check . --format sarif > qawrench.sarif
```

## Try it on the demo artifacts

[`examples/check/`](../examples/check/) is a small synthetic artifacts folder:
a Playwright JSON report, a trace, a HAR recording, an axe report, three
Lighthouse runs and a lychee report with the Markdown files it checked. Every
URL in it uses an `example.test` domain; nothing was recorded from a real site.

```sh
cd examples/check
qawrench check
```

It reports 8 artifacts, 2 errors, 7 warnings and 5 info notices, among them:

```text
Tests & flakiness
  test-results/results.json  Playwright JSON report
    error  flaky/failed                    search.spec.ts › search › shows suggestions [chromium]: failed on all 3 attempts (failed, timedOut, failed)
    warn   flaky/flaky                     checkout.spec.ts › checkout › applies a coupon [chromium]: failed, then passed on retry 1 (attempts: failed, passed)

Accessibility & performance
  lighthouse/lhr-1.json  Lighthouse report
    warn   lighthouse/noisy                https://shop.example.test/ (mobile): 3 runs: TBT varies more than 10% of the median; TBT 210 ms (MAD 60 ms, range 150 ms–420 ms); ...

Pages & links
  docs/testing.md
    error  links/http-failure              https://docs.example.test/retries: Network error (HTTP 404)  line 5
```

`tests/check/test_cli.py::test_readme_demo_output` pins these lines.

## What is detected

Artifacts are recognised by their **content**, not their name, so you can point
`check` at a whole workspace. Only `*.json`, `*.har`, `*.zip` and `*.xml` files
and Crawlee dataset folders are opened. Dot-directories (except `.lighthouseci` and `.statewatch`),
`node_modules`, `venv`, `site-packages` and `__pycache__` are skipped, and
symlinked directories are not followed. JSON files over 64 MiB are not read.

| Artifact | Recognised by |
| --- | --- |
| Playwright JSON report | an object with `config`, `suites` and `stats` (the `json` reporter) |
| CTRF report | `results.tool` and `results.tests` (playwright-ctrf-json-reporter) |
| JUnit XML | a `<testsuites>` or `<testsuite>` root element |
| Playwright trace | a ZIP with a `*.trace` stream (`trace.zip`); identical copies, such as the ones an HTML report keeps under `data/`, are counted once |
| HAR | `log.entries` (any `*.json` or `*.har`), or a `*.har.zip` |
| axe-core results | `testEngine.name` = `axe-core` and a `violations` array, or an array of such results |
| Pa11y CI results | the combined JSON: `total`, `passes`, `errors` and a `results` object |
| a11y baseline | a snapshot written by `qawrench a11y baseline` (`kind: a11y-baseline`) |
| Lighthouse report | `lighthouseVersion` and `audits` (an LHR, as LHCI writes to `.lighthouseci/lhr-*.json`) |
| lychee report | `error_map` and `success_map` (`lychee --format json`) |
| pytest statewatch report | `schema_version` 1 with `tests`, `first_observed_writers` and `events_file`: the `report.json` that `python -m pytest --statewatch POLICY.json` writes (default `.statewatch/`). Reports of confirmation replays (`replays/<run id>/<NNN>/report.json`) are summarised by their parent report and skipped unless named explicitly |
| Parquet export | `*.parquet.manifest.json` written by `qawrench crawlee-export` |
| Crawlee dataset | a `datasets/<name>/` folder of numbered `*.json` items |

Files that are not artifacts (`package.json`, a tsconfig, HTML report assets)
are skipped silently. A JSON file whose name looks like a report (it contains
`result`, `report`, `lighthouse`, `axe`, `pa11y`, `lychee`, `ctrf`, `junit` or
`baseline`) but does not parse is reported as `parse/invalid-json`, because a
truncated upload would otherwise vanish from the report. Files you name
explicitly are always reported if they cannot be parsed or are not recognised.

## Checks

`qawrench check --list-checks` prints this list. All checks run by default.

| Area | Check | Uses | Reports |
| --- | --- | --- | --- |
| Tests & flakiness | `flaky` | `qawrench flaky` (report adapters and outcome classification) | `flaky` failed then passed on retry, `failed` failed on every attempt, `unexpected-pass` a `test.fail()` test passed, `interrupted`, `global-errors` outside any test, `summary` |
| | `har` | `qawrench har` (`build_plan`) | `ambiguous-replay` the same method, URL and body recorded with different responses, `volatile-query-key` a query key whose value changes on every request to a path, `summary` |
| | `trace-budget` | `qawrench trace-budget` (`inspect_archive`) | `over-budget` (with `--trace-budget`), `missing-resources` screenshot frames or network bodies referenced but not in the archive, `stored-uncompressed`, `size` by category, `invalid` |
| | `statewatch` | `qawrench statewatch` (report validation) | `state-leak` a test left watched environment, cwd or module state changed, `order-dependent` a failure reproduced after an earlier test but not alone, `between-tests` a change outside any test, `incomplete` state that could not be observed, `interrupted`, `confirmation` other replay results, `not-observed`, `summary` |
| Accessibility & performance | `a11y` | `qawrench a11y` (`make_baseline`, `compare`) | `new-violation` against a baseline found among the artifacts, otherwise `violations` by impact; `baseline-delta`, `clean` |
| | `lighthouse` | `qawrench lighthouse` (LHR validation and median/MAD) | `noisy` a metric's spread is over 10% of its median, `stable`, `too-few-runs`, `runtime-error`, `invalid-metric` |
| Pages & links | `links` | `qawrench links` (`convert`) | `http-failure`, `transport-error`, `timeout`, each on the file that contains the link and, for Markdown, its line; `summary` |
| | `page-diff` | | not run by `check`: needs a before/after pair and a rules file |
| | `structured-data` | | not run by `check`: needs two captures of a page and its URL |
| Crawling | `crawlee-export` | `qawrench crawlee-export` (`verify_export`) | `verification-failed` a Parquet file that does not match its manifest, `verified`, `needs-schema` for raw datasets |
| | `frontier` | | not run by `check`: needs a request list and a URL policy |
| Inputs | `parse` | (built in) | artifacts that cannot be read or parsed |

Rule ids are `check/rule`, for example `flaky/flaky` or `links/http-failure`;
the JSON and SARIF output use the same ids. Every check also has
`unsupported`-style info rules for inputs outside what its tool reads (for
example a Playwright JSON report from another version, see below).

Checks that need configuration are listed but do nothing by default. Ask for
one with `--only page-diff` and `check` prints the command to run instead.

### Severities

- **error**: the run is wrong as it stands: a test failed on every attempt, a
  link returns an HTTP error, a new serious or critical accessibility
  violation, a Parquet export that does not match its manifest.
- **warning**: the result cannot be trusted as-is: a test that only passed on
  retry, a request recorded with two different responses, a trace with missing
  frames, a test that leaves watched state changed or fails only after another
  test, Lighthouse numbers too noisy to compare, accessibility violations with
  no baseline, a link that timed out.
- **info**: worth knowing, not a defect: summaries, trace sizes, query keys
  that change per request, inputs a check does not read.

`--fail-on` (default `error`) picks the level that makes `check` exit 1.

### How the checks decide

- **flaky** reads Playwright's JSON reporter output (the Playwright 1.56.1
  dialect) and playwright-ctrf-json-reporter 0.0.29 CTRF. Partial reports
  (missing attempts, counts that do not add up) are refused, not guessed at.
  JUnit XML does not record individual attempts portably, so it only gets a
  hint to add the JSON reporter. For history across runs, import the same
  report into `qawrench flaky`.
- **har** reads HAR 1.2 with embedded bodies (Playwright `recordHar` to a
  `.har` file) or a self-contained `.har.zip`. It plans an empty rewrite with
  `qawrench har`: two entries with the same method, URL and body but different
  responses are an ambiguous replay. For each query key whose value differs on
  every request to the same path, it plans dropping that key; the key is
  reported only if that merges requests without creating conflicting responses.
  URLs are printed without their query string.
- **trace-budget** validates every member of a version-8 trace (CRCs, event
  envelopes) and reports where the bytes go. Without `--trace-budget SIZE`
  (`25MB`, `500kB` or a byte count; SI units) it only reports sizes.
- **statewatch** reads the `report.json` (schema version 1) written by the
  statewatch pytest plugin (`qawrench[statewatch]`); the check itself needs
  no extra and never runs pytest. A test whose protocol ended with unallowed,
  unrestored changes to the watched state is a `state-leak`, naming the
  fields (never their values) and whether the test is their first observed
  writer: a test boundary, fixtures included, not the line that made the
  change. A failing test that passed alone and failed after an observed
  writer in `--statewatch-confirm` replays is `order-dependent`; that is one
  clean run and one failing pair, not proof that a field caused it. Fields
  that could not be read make the observation `incomplete`. Replay commands
  are not copied into the findings; `qawrench statewatch REPORT` prints them.
  See [statewatch.md](statewatch.md).
- **a11y** reads complete axe-core 4.10.3 / 4.11.0 results and Pa11y CI 4
  combined JSON. Pa11y CI output carries no version, so `check` reads it as
  4.0.0. Scans with errors or `incomplete` results are not counted as clean.
  If an `a11y-baseline` snapshot with the same engine, version, page set and
  options is among the artifacts, only new violations are reported, severity
  by impact (critical and serious are errors).
- **lighthouse** reads Lighthouse 12.8.2 navigation reports with explicit
  config settings, groups runs by URL, form factor, settings and folder (a
  `baseline/` and a `current/` folder are never pooled) and reports the median
  and median absolute deviation (MAD) of FCP, LCP, TBT, CLS and Speed Index.
  With fewer than 3 runs it says so. A metric whose MAD is more than 10% of its
  median is `noisy`: a single-run comparison of it would mostly measure noise.
  To gate a regression with explicit tolerances, use
  `qawrench lighthouse BASELINE_DIR CURRENT_DIR --config gate.json`.
- **links** needs the files lychee checked. It looks for them in the current
  directory, then next to the report; `--source-root DIR` names the folder
  lychee ran in. When every failing source is Markdown, links are located to
  their line; otherwise findings are per file. Only lychee 0.18 JSON for local
  files is read (no cache, no redirect counters).
- **crawlee-export** verifies an exported Parquet file against its manifest
  (schema, row count, digests). It needs `pyarrow`: without it the check
  reports `missing-dependency` with the install command
  (`pipx install 'qawrench[parquet]'`).

## Output

### Text (default)

Findings are grouped by area, then by artifact, most severe first, and end
with a one-line summary:

```text
8 artifacts · 2 errors · 7 warnings · 5 info
```

`-v` adds a fix hint and the location inside the artifact (for example
`/log/entries/2`) under each finding. Colour is used on a terminal unless
`NO_COLOR` is set; force it with `--color always|never`. Text from artifacts is
printed with control characters escaped.

### Markdown (`--format markdown`)

A summary line, then one table per area with errors and warnings, and the info
notices folded into a `<details>` block. It is meant for a PR comment or the
GitHub Actions job summary:

```yaml
- name: Check test artifacts
  if: always()
  run: uvx qawrench check test-results/ --format markdown >> "$GITHUB_STEP_SUMMARY"
```

Cells are escaped (pipes, HTML, `@mentions`) and each area shows at most 40
rows, so the output stays under GitHub's comment size limit.

### JSON (`--format json`)

```json
{
  "format": "qawrench.check/v1",
  "tool": {"name": "qawrench", "version": "0.1.0"},
  "paths": ["."],
  "checks": ["flaky", "har", "trace-budget", "statewatch", "a11y", "lighthouse", "links", "..."],
  "artifacts": [{"path": "test-results/results.json", "kind": "playwright-report",
                 "label": "Playwright JSON report", "copies": 0, "findings": 4}],
  "findings": [{
    "check": "flaky", "rule": "flaky", "ruleId": "flaky/flaky", "severity": "warning",
    "area": "Tests & flakiness", "message": "failed, then passed on retry 1 (attempts: failed, passed)",
    "file": "test-results/results.json", "line": null, "pointer": null,
    "subject": "checkout.spec.ts › checkout › applies a coupon [chromium]", "hint": "..."
  }],
  "summary": {"artifacts": 8, "inspected": 8, "error": 2, "warning": 7, "info": 5},
  "failOn": "error",
  "failed": true
}
```

### SARIF (`--format sarif`)

SARIF 2.1.0 for GitHub code scanning. Broken links point at the Markdown file
and line; other findings point at the artifact. Run-level notices become tool
execution notifications.

## Exit status

| Code | Meaning |
| --- | --- |
| 0 | No finding at or above `--fail-on` |
| 1 | At least one finding at or above `--fail-on` |
| 2 | Usage or input error: unknown option or check, missing path |

A check that crashes becomes one `internal` error finding for that artifact;
the other checks still run. Set `QAWRENCH_CHECK_DEBUG=1` to get the traceback
instead.

## Library use

```python
from qawrench.check import CheckOptions, discover, run_checks, select

discovery = discover(["test-results/"])
findings = run_checks(discovery, select(only=["flaky", "trace-budget"]),
                      CheckOptions(trace_budget=25_000_000))
for finding in findings:
    print(finding.severity, finding.rule_id, finding.file, finding.message)
```

## Limits

- `check` sees artifacts, not your test suite: it cannot re-run a test, and
  "flaky" means "failed then passed in this run". Use `qawrench flaky` to keep
  a history across runs.
- Each tool reads a strict, documented dialect and refuses inputs it cannot
  account for completely (an info finding says why). Newer Playwright,
  Lighthouse or lychee versions may need a tool update first.
- The 10% Lighthouse spread threshold is a fixed heuristic, not a statistical
  test.
