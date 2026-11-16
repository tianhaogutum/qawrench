# qawrench flaky

`qawrench flaky` (formerly `retry-history-ledger`) imports native Playwright JSON and
Playwright CTRF reports into a local SQLite history. It retains available attempts,
exposes missing retry details, and identifies recurring fail-then-pass tests across
runs. No browser, network access, service, or third-party runtime package is needed.

Playwright's [JSON/HTML reporters](https://playwright.dev/docs/test-reporters)
provide per-run artifacts; the [Playwright CTRF reporter](https://github.com/ctrf-io/playwright-ctrf-json-reporter/tree/v0.0.29)
already provides common-format output. This tool complements them with small,
transactional local storage across both inputs, explicit loss accounting, and
idempotent imports. Deterministic ledger bundles merge histories collected by
separate CI workers without duplicating shared run IDs. It does not claim to be
the first historical test dashboard.

## Install

Requires Python 3.11+ with SQLite (tested with SQLite 3.46.1).

```bash
pipx install qawrench          # or: pip install qawrench
qawrench flaky --version       # prints the bare qawrench version

# Or run without installing:
uvx qawrench flaky --help
```

`python -m qawrench flaky` is equivalent to `qawrench flaky`. This command has no
third-party runtime dependencies.

The example reports below live in `examples/flaky/` in the qawrench repository; run
the commands from the repository root (or adjust the paths).

## Try the synthetic offline examples

Use a fresh database path for a new experiment. The CLI will never overwrite an
existing run with different content. All reports in `examples/flaky/` are **synthetic**,
not results from a real browser or site.

```bash
qawrench flaky import --format playwright --run-id pw-1 --db /tmp/flaky-ledger.sqlite examples/flaky/playwright.json
qawrench flaky import --format ctrf --project chromium --run-id ctrf-1 --db /tmp/flaky-ledger.sqlite examples/flaky/ctrf.json
qawrench flaky report --db /tmp/flaky-ledger.sqlite --format json
qawrench flaky report --db /tmp/flaky-ledger.sqlite --format markdown
```

**CLI clarification to the specification:** CTRF imports require `--project`.
The inspected reporter does not emit a separate project field. Its legacy
`suite` joins titles with ` > `, which cannot be split unambiguously. Supply the
actual project name and import a report scoped to that project. We preserve
`suite` as an opaque title-path component rather than guess project or suite
boundaries. Native Playwright imports get project name/ID from the report.

The first import reports 8 test observations and 9 available attempts. The CTRF
import reports 6 observations and 7 available attempts. The summary includes:

```json
{
  "run_count": 2,
  "unique_tests": 14,
  "totals": {
    "observations": 14,
    "available_attempts": 16,
    "retried_tests": 4,
    "reported_retries": 5,
    "retry_incidence": 0.2857142857142857,
    "fail_then_pass": 1,
    "mapped_fail_then_pass": 1,
    "terminal_failure": 2,
    "skipped": 2,
    "interrupted": 2,
    "not_run": 1,
    "attempt_details_unavailable": 2,
    "available_attempt_duration_ms": 116,
    "attempt_duration_complete": false
  }
}
```

This is an excerpt, not the complete JSON output. The native failed-then-passed
`checkout` is flaky; the same title in Firefox is a separate identity. Skipped
tests are never counted as passes. The CTRF aggregate reports two retries but
only one available final attempt. Its missing attempts are **not synthesized**.
An unexecuted native test has zero attempt rows and is separately `not_run`.

Reimporting the exact same bytes with the same format/project/run ID prints
`"action": "unchanged"` without modifying the database. Changed bytes (including
whitespace), a changed format, or a changed project context under the same run ID
exit 2 with a conflict. Use a new run ID for an independent run:

```bash
qawrench flaky import --format playwright --run-id pw-2 --db /tmp/flaky-ledger.sqlite examples/flaky/playwright.json
qawrench flaky report --db /tmp/flaky-ledger.sqlite --format markdown
```

Now the native `checkout` identity has fail-then-pass evidence in two runs and
appears in the recurring section. Reusing synthetic input illustrates storage
semantics; it does not establish real-world flakiness or performance gains.

Validate without opening/creating/querying any database:

```bash
qawrench flaky import --format playwright --run-id preview --db /tmp/flaky-ledger.sqlite --dry-run examples/flaky/playwright.json
```

Dry run validates input only; it cannot predict a run-ID conflict or database
write failure. It never executes scripts or opens attachment paths.

Recover an exact original report, including unknown ancillary fields:

```bash
qawrench flaky export --db /tmp/flaky-ledger.sqlite --run-id pw-1 > recovered-pw.json
```

## Merge histories from two workers

`export-ledger` writes a deterministic v1 JSON bundle to stdout. `import-ledger`
validates every embedded report and merges the **whole bundle in one SQLite
transaction**. The existing `export --run-id` command still recovers one raw
upstream report; it does not export a ledger bundle.

This local synthetic example uses fresh temporary paths. Worker A has `shared`
and `worker-a`; worker B has the identical `shared` run and `worker-b`:

```bash
demo=$(mktemp -d)
qawrench flaky import --format playwright --run-id shared --db "$demo/a.sqlite" examples/flaky/playwright.json
qawrench flaky import --format playwright --run-id worker-a --db "$demo/a.sqlite" examples/flaky/playwright.json
qawrench flaky import --format playwright --run-id shared --db "$demo/b.sqlite" examples/flaky/playwright.json
qawrench flaky import --format ctrf --project chromium --run-id worker-b --db "$demo/b.sqlite" examples/flaky/ctrf.json
qawrench flaky export-ledger --db "$demo/a.sqlite" > "$demo/a.json"
qawrench flaky export-ledger --db "$demo/b.sqlite" > "$demo/b.json"
qawrench flaky import-ledger --db "$demo/merged.sqlite" "$demo/a.json"
qawrench flaky import-ledger --db "$demo/merged.sqlite" "$demo/b.json"
qawrench flaky import-ledger --db "$demo/merged.sqlite" "$demo/b.json"
qawrench flaky report --db "$demo/merged.sqlite"
```

The three merge responses are exactly:

```json
{"action": "merged", "imported_runs": 2, "unchanged_runs": 0}
{"action": "merged", "imported_runs": 1, "unchanged_runs": 1}
{"action": "unchanged", "imported_runs": 0, "unchanged_runs": 2}
```

The summary has 3 runs, 22 test observations and 25 available attempts. Its native
fail-then-pass test recurs in `shared` and `worker-a`. Identical source bytes under
different run IDs are separate runs; callers must assign a globally consistent
ID to the same physical run across workers. This tool cannot infer run identity
from matching test outcomes.

If an incoming ID already exists with different raw bytes (even whitespace),
format, or project context, the CLI exits 2 and writes a conflict diagnostic to
stderr. **No runs from that bundle are added**, including runs that sorted before
the conflict. An SQL write failure also rolls back the whole bundle. Previous
successful bundle imports remain committed. Identical duplicates within one
bundle count once; conflicting duplicates are rejected before opening SQLite.
Malformed bundles and invalid embedded reports likewise leave a missing database
uncreated and an existing database untouched.

To transfer selected runs, repeat `--run-id` on `export-ledger`. Using worker B
from the example above:

```bash
qawrench flaky export-ledger --db "$demo/b.sqlite" --run-id worker-b --run-id shared --run-id worker-b > "$demo/selected.json"
qawrench flaky import-ledger --db "$demo/selected.sqlite" "$demo/selected.json"
qawrench flaky export-ledger --db "$demo/selected.sqlite" > "$demo/selected-again.json"
cmp "$demo/selected.json" "$demo/selected-again.json"
```

The bundle contains exactly `shared`, then `worker-b`, once each. The import
reports `{"action": "merged", "imported_runs": 2, "unchanged_runs": 0}`, and
`cmp` succeeds: re-export preserves every bundle byte. Source reports and adapter
contexts also roundtrip exactly. Selection uses exact, case-sensitive IDs without
trimming or pattern matching; flag order and duplicate IDs do not affect output.
Omitting `--run-id` exports all runs as before.

Unknown IDs reject the entire selection with exit 2 and a sorted list of unknown
IDs on stderr. Blank IDs also fail. **Selection/source validation failures write
no stdout bytes**, even when other selected IDs are valid. Export never modifies
the source ledger.
For example, `export-ledger --db "$demo/b.sqlite" --run-id shared --run-id missing`
fails with `unknown run-id(s): 'missing'`. Shell `>` redirection can still create
or truncate the destination file before the command starts.

The public bundle contract is:

* The root has exactly `ledger_format: "retry-history-ledger"`, integer
  `version: 1`, and a `runs` array. Each run has exactly `run_id`, `format`,
  `project_context` (`null` for native Playwright, a project string for CTRF),
  `sha256` (lowercase hex of the original report bytes), and `raw_report_base64`
  (canonical RFC 4648 base64 of those exact bytes).
* Exports sort runs by run ID, use ASCII JSON with sorted object keys and compact
  separators, and end with one LF. Import order, local `imported_at` timestamps,
  and SQLite row order cannot change bundle bytes. Imports accept other JSON
  whitespace/key/run ordering, then exports canonicalize it.
* Digests and base64 are checked. Unknown fields, duplicate JSON keys, unsupported
  versions, missing contexts, and unsupported source dialects fail explicitly.
  Checksums detect corruption; they do not authenticate a producer.
* Normalized observations/attempts are regenerated through the existing pinned
  adapters. This transfers logical history and exact source reports, not a
  byte-for-byte SQLite backup or local import timestamps. No schema migration is
  needed. An empty bundle creates a valid empty ledger and reports zero runs
  imported. Export opens the source read-only, in a single snapshot transaction.

Bundles, like reports, are loaded into memory; use them for modest artifacts.
They contain all original logs/ancillary metadata. Each import is local and
offline, with no external attachment reads or executable content.

## Retain statistics and archive old attempts

`prune` keeps full details for the newest N local imports and moves older full
runs into a **new v1 ledger bundle file**. It retains run/test identities,
per-observation outcome flags, retry/loss counters, available-attempt counts and
duration sums. All global/per-run totals and recurring-flake histories remain
unchanged. The archive contains the exact original reports, from which the
pinned adapters can rebuild the full attempts.

This example uses three synthetic runs and a fresh directory:

```bash
demo=$(mktemp -d)
qawrench flaky import --format playwright --run-id 001-pw --db "$demo/history.sqlite" examples/flaky/playwright.json
qawrench flaky import --format ctrf --project chromium --run-id 002-ctrf --db "$demo/history.sqlite" examples/flaky/ctrf.json
qawrench flaky import --format playwright --run-id 003-pw --db "$demo/history.sqlite" examples/flaky/playwright.json
qawrench flaky prune --db "$demo/history.sqlite" --keep-latest 1 --archive "$demo/old-runs.json" --dry-run
qawrench flaky prune --db "$demo/history.sqlite" --keep-latest 1 --archive "$demo/old-runs.json"
qawrench flaky report --db "$demo/history.sqlite"
# Recover the archived full history in a separate ledger:
qawrench flaky import-ledger --db "$demo/recovered.sqlite" "$demo/old-runs.json"
qawrench flaky export --db "$demo/recovered.sqlite" --run-id 001-pw > "$demo/original-pw.json"
```

The dry-run JSON has `action: "planned"`, `run_ids: ["001-pw", "002-ctrf"]`,
`observations: 14`, `attempts: 16`, the original `source_bytes` count and the
absolute `archive_path`. It opens SQLite read-only and writes nothing. It does
not check archive writability, source checksums or available disk space. Apply
recomputes the plan under a write lock; imports between commands can change it.
The successful response has `action: "pruned"` and adds `archive_sha256`.
The live ledger still reports **3 runs, 22 observations, 25 historically available
attempts, 2 native fail-then-pass observations and one recurring native test**;
only the newest run's 9 attempt rows remain locally. Recovering the archive
produces 2 full runs, 14 observations and 16 attempt rows with exact source bytes.

Retention behavior and audit contract:

* Order is local `imported_at` UTC timestamp descending, then exact `run_id`
  descending for ties. Upstream execution dates are not used. Archived runs keep
  their place in this order. `--keep-latest 0` archives all remaining full runs;
  negative counts fail. Increasing the count does not restore earlier archives.
* Archived observations have `attempts: []`, `attempts_archived: true` and
  `available_attempt_count`. Their existing `attempt_details_unavailable` flag
  continues to describe upstream loss, independently of archival. The unchanged
  `available_attempts`/duration totals include historical evidence in the archive.
  Each archived run exposes `archive.path`, `archive.sha256` (the whole bundle)
  and `archive.source_sha256` (that run's original report). SHA-256 detects
  corruption, not producer authenticity. `verify-archive` replays an explicitly
  supplied bundle and compares it with the retained statistics read-only.
* Apply holds `BEGIN IMMEDIATE`, checks source digests, writes and fsyncs a
  temporary sibling file, publishes it atomically without replacing any existing
  path, and fsyncs its directory **before changing SQLite**. It then removes
  archived attempt rows, observation source objects and original report blobs in
  one transaction. Run metadata (including global errors) remains. Use a local
  filesystem supporting hard links, fsync and SQLite locking; no directories are
  created. Database paths and their SQLite sidecars are rejected as archive paths.
* Archive or SQL errors exit 2 and preserve the original database. A complete
  archive may remain after directory-sync/SQL/commit failure; it is safe to
  inspect/import. Choose a new filename when retrying. Existing archive files
  are never overwritten, even if their content matches. With no eligible runs,
  apply returns `action: "unchanged"`, creates no archive and changes nothing.
* The first successful nonempty prune explicitly upgrades schema v1 to v2 in the
  same transaction, adding `runs.archive` and compact per-observation retained
  metrics. Reports/imports support both versions; read-only/dry-run/no-op commands
  never migrate. Older package versions cannot open a v2 ledger. Normal imports
  remain idempotent for archived IDs and do not restore their details.
* `export --run-id` for an archived run and whole-ledger `export-ledger` for any
  pruned ledger fail explicitly with an archive reference. With repeatable
  `export-ledger --run-id`, selecting only full recent runs succeeds; selecting
  any archived run fails with empty stdout and its archive reference. No export
  silently omits requested runs or automatically reads external paths. Keep the
  archive files: the database alone cannot recover pruned full evidence.
* Pruning frees SQLite pages for reuse; it does **not** VACUUM, shrink the file,
  securely erase freed pages, or bound retained run/test metadata. No storage-size
  or speed improvement is claimed. Reports, source bundles and pruning inputs
  are loaded in memory and remain intended for modest local histories.

Library equivalent:

```python
from qawrench.flaky import prune

plan = prune("history.sqlite", keep_latest=20, archive_path="older.json", dry_run=True)
result = prune("history.sqlite", keep_latest=20, archive_path="older.json")
```

## Find retained archive references

`list-archives` helps select an explicit bundle for `verify-archive`. Using the
retention example above:

```bash
qawrench flaky list-archives --db "$demo/history.sqlite"
```

Success exits 0 with one deterministic JSON object (digest/path abbreviated):

```json
{"action": "listed", "archives": [{"archive_sha256": "<64 lowercase hex characters>", "attempts": 16, "observations": 14, "paths": ["/absolute/path/old-runs.json"], "run_ids": ["001-pw", "002-ctrf"]}]}
```

Groups sort by archive SHA-256. Each group has sorted exact run IDs and sorted,
deduplicated stored paths. Separate prune operations normally produce separate
groups; different stored paths with the same digest remain in one group. Counts
sum archived observation rows and their retained `available_attempts` values,
including upstream evidence gaps. A zero-observation archived run still appears
with zero counts. Full local runs are excluded. An unpruned v1 ledger returns
exactly `{"action": "listed", "archives": []}` without migration.

One read-only snapshot supplies references and counts. Each archived reference
must have exactly `path`, `sha256` and `source_sha256`, a nonblank string path,
and lowercase 64-hex digests. Its source digest must agree with the stored run
digest. Each archived observation must contain `retained_attempts` with a
nonnegative integer `available_attempts` at most 2^53-1; booleans do not count as
integers. Malformed JSON, references or counts, missing databases and incompatible
ledgers exit 2 with explicit stderr and empty stdout. No bytes or schema change.

Paths are informational strings: they are never resolved, opened or scanned.
Missing, moved or corrupt bundle files do not affect this inventory. It does
**not** establish that a bundle exists or is valid, replay metadata/classifications,
or detect missing observation rows or plausible rewritten counts. Use
`verify-archive --db DATABASE EXPLICIT_BUNDLE_PATH` to audit archived evidence,
or `verify-run` for full local evidence. The inventory reads retained JSON in
memory and makes no large-history performance claim.

Library equivalent: `from qawrench.flaky import list_archives`, then
`list_archives("history.sqlite")`. Library errors raise `LedgerError`.

## Find exact run IDs for an audit

`list-runs` lists full and archived runs without replaying their evidence. It
supports schema v1/v2 and optional `--state all|full|archived` (default `all`).
Using the three-run retention example above:

```bash
qawrench flaky list-runs --db "$demo/history.sqlite"
qawrench flaky list-runs --db "$demo/history.sqlite" --state full
qawrench flaky list-runs --db "$demo/history.sqlite" --state archived
```

The full-only response is exactly this shape (digest abbreviated):

```json
{"action": "listed", "runs": [{"attempts": 9, "format": "playwright", "observations": 8, "project_context": null, "run_id": "003-pw", "source_sha256": "<64 lowercase hex characters>", "state": "full"}]}
```

All-state output also includes `001-pw` (8 observations, 9 available attempts)
and `002-ctrf` (6 observations, 7 available attempts, `project_context:
"chromium"`), each with `state: "archived"` and an `archive_sha256` field.
Full runs omit that field. Run IDs sort exactly and case-sensitively, preserving
whitespace. Zero-observation runs appear with zero counts; an empty ledger or
filter with no matches returns `{"action": "listed", "runs": []}`.

One read-only snapshot supplies selected run IDs, format/project context, stored
source SHA-256, archive references and counts. Full-run counts are local SQL
observation/attempt row counts; archived counts sum retained available attempts,
preserving upstream evidence gaps. Selected IDs must be nonblank strings, formats
must be supported, project contexts must be null for native or nonblank for CTRF,
and source digests must be lowercase 64-hex strings. Archived references and
counts follow the same strict validation as `list-archives`. Invalid selected
fields/counts, invalid filters and missing/incompatible databases exit 2 with
explicit stderr, empty stdout and unchanged database bytes. Filters skip
unselected runs before validating their fields and counts.

This is an inventory, **not verification**. It does not read source blobs, run
metadata, full observation/attempt JSON or external paths; it neither replays
reports nor checks actual source/bundle integrity or row completeness. Plausible
rewritten counts or coordinated reference changes can pass. Use the discovered
exact ID with `verify-run`, or select an explicit bundle for `verify-archive`.
No schema migration, scanning, repair or restoration occurs. Results/archived
retained JSON remain in memory, intended for modest histories.

```python
from qawrench.flaky import list_runs

all_runs = list_runs("history.sqlite")
full_runs = list_runs("history.sqlite", state="full")
```

## Verify retained statistics against an archive

`verify-archive` audits one explicitly supplied v1 bundle against a read-only
SQLite snapshot. Using the retention example above:

```bash
qawrench flaky verify-archive --db "$demo/history.sqlite" "$demo/old-runs.json"
```

Success exits 0 and prints one JSON object (the digest here is abbreviated):

```json
{"action": "verified", "archive_sha256": "<64 lowercase hex characters>", "attempts": 16, "observations": 14, "run_ids": ["001-pw", "002-ctrf"]}
```

The digest covers the **exact supplied bundle bytes**. Run IDs are sorted;
`attempts` counts historically available attempts replayed from the archive,
not the now-empty local attempt table. A run containing zero observations is
valid and contributes zero observations/attempts.

Verification checks every embedded source digest and replays reports through
the same pinned Playwright/CTRF adapters used for import. Every bundle run must
exist and be archived in this ledger; every ledger reference to this bundle's
digest must be present. For each run it checks both stored source SHA-256
references, the stored bundle SHA-256, adapter format/project context, all run
metadata (including wall duration, test count and global errors), exact
observation identities, and **every retained observation field**. This includes
retry/loss flags, reported durations, each outcome classification, available
attempt counts and duration sums. These are all inputs to the derived per-run
totals and recurring histories. Archived source blobs and local attempt rows
must also be empty. Run import timestamps are local bookkeeping and are not in
the bundle, so they cannot be replay-verified.

A moved byte-identical bundle works. Stored archive paths are informational:
the command never follows them, opens attachments, creates a recovery database,
migrates the schema or modifies the ledger/archive. Other archive files are not
read. It scans stored archive references to establish complete membership, so
malformed reference JSON is rejected. Only runs in the supplied archive have
their metadata/metrics replay-checked; full runs and other archives need separate
audits. Duplicate identical bundle run entries retain normal v1 deduplication.

Missing files/databases, wrong/corrupt bundles, malformed reports, mismatched
hashes/context/metrics, missing/extra/unknown/full runs, malformed retained JSON,
and empty bundles fail with **exit 2, explicit stderr and empty stdout**. Nothing
is repaired or restored. A v1 ledger containing only full runs cannot verify an
archive. Reformatting a bundle changes its digest even if its sources are
unchanged. SHA-256 detects corruption, not authenticity: coordinated changes to
a bundle and its retained references/evidence, or deleted ledger records with
no surviving reference, cannot be proven from these artifacts alone. The bundle
and replay are held in memory, intended for modest local histories.

Library equivalent:

```python
from qawrench.flaky import verify_archive

result = verify_archive("history.sqlite", "older.json")
```

## Verify one full local run

`verify-run` checks one exact, case-sensitive run ID in a read-only SQLite
snapshot. For the synthetic imports in the first example, before pruning:

```bash
qawrench flaky verify-run --db /tmp/flaky-ledger.sqlite --run-id pw-1
qawrench flaky verify-run --db /tmp/flaky-ledger.sqlite --run-id ctrf-1
```

Success exits 0 with deterministic JSON (source digest abbreviated here):

```json
{"action": "verified", "attempts": 9, "observations": 8, "run_id": "pw-1", "source_sha256": "<64 lowercase hex characters>"}
```

The CTRF example returns 6 observations and 7 available attempts. Missing retry
details remain missing; a zero-observation run verifies with both counts zero.
The command checks the SHA-256 of the original stored source bytes, replays the
pinned adapter with the stored format/project context, and compares all run
metadata, observation identities and fields (including source objects), and
every available attempt's identity and complete JSON. SQL attempt indices,
statuses and durations must independently match replay, so changing a column
while leaving its JSON untouched fails. SQLite REAL durations compare by numeric
value; JSON flags, counts and durations retain their exact replayed types/values.

Unknown/blank IDs, corrupt sources, malformed reports or retained JSON, and
missing/extra/tampered metadata, observations or attempts fail with **exit 2,
explicit stderr and empty stdout**. Archived IDs fail with guidance to use
`verify-archive` with an explicit bundle path. Full runs in both schema v1 and
v2 are supported. Neither command repairs evidence, migrates storage or reads
attachment/stored archive paths, and both leave database/archive bytes unchanged.

Only the selected run is audited. Unrelated runs and local `imported_at`
timestamps are outside the check. SHA-256 cannot authenticate a producer or
detect coordinated source/digest/evidence changes. Source and replay remain
in memory, intended for modest local histories.

```python
from qawrench.flaky import verify_run

result = verify_run("history.sqlite", "pw-1")
```

## Report selected runs

Use exact IDs from `list-runs` to scope JSON or Markdown history reports:

```bash
qawrench flaky report --db /tmp/flaky-ledger.sqlite --run-id pw-1 --run-id ctrf-1
qawrench flaky report --db /tmp/flaky-ledger.sqlite --run-id pw-1 --format markdown
```

Repeat `--run-id` for multiple runs. IDs are case-sensitive and remain literal,
including whitespace; use `--run-id=--literal` for an ID starting with `--`.
Order and duplicates do not affect output. Omitting the flag reports all runs.
The existing report shape is preserved: runs are sorted by exact ID, and totals,
unique tests, histories and recurrence are computed **only within the selection**.
For the native/CTRF examples above, selecting both gives 2 runs, 14 observations
and 16 available attempts; selecting only `pw-1` gives 1 run, 8 observations,
9 available attempts and no recurring fail-then-pass test.

Full and archived runs can be mixed. Archived counts/classifications remain
retained historical evidence, with `attempts_archived: true`, empty `attempts`
and `available_attempt_count`; upstream gaps still use
`attempt_details_unavailable`. Zero-observation selected runs still count as
runs. No archive or attachment path is opened, and no source replay occurs.
This is a summary of stored evidence, not an integrity audit.

```python
from qawrench.flaky import report

selected = report("history.sqlite", run_ids=["pw-1", "ctrf-1", "pw-1"])
empty = report("history.sqlite", run_ids=[])
```

`run_ids=None` retains all-run behavior. Other values must be iterables of
nonblank UTF-8 encodable strings; scalar strings/bytes are rejected. An empty
library selection returns the existing empty report shape and actual schema
version. Invalid or unknown IDs fail with `LedgerError` (CLI **exit 2, explicit
stderr, empty stdout**), leaving DB bytes unchanged. Membership and report rows
share one read-only v1/v2 snapshot; unknown IDs are rejected before selected
JSON is decoded. Unselected evidence is not decoded, so its corruption does not
block an explicitly scoped report. Selected malformed JSON still fails; this
command does not add strict replay validation or repair for stored evidence.
Every selected observation's `retries` must be an integer from `0` through
`2^53-1`, matching `compare-runs` and `test-history`. Missing/null values,
booleans (including `false`), strings, floats (including `1.0`), fractions,
negative and larger counts fail with run ID, test ID and `retries` in the error.
This applies to native/CTRF full and archived observations in v1/v2 ledgers.
Valid retries are preserved without coercion; available/retained counts and
upstream detail gaps keep their existing meaning. For example, a selected
stored `retries: true` raises `LedgerError`; JSON and Markdown CLI commands exit
2 with empty stdout and unchanged DB bytes. Selecting only another valid run
still succeeds. This checks the count type/range, not its agreement with source
bytes or attempt inventory; use explicit replay verification for that audit.
The selected observation fields `reported_flaky`, `attempt_details_unavailable`
and `status_detail_unavailable` must each be present and contain a JSON boolean
(`true` or `false`), matching comparison and history. Integers (including `0` and
`1`), floats, strings, null, missing fields, arrays and objects fail with run ID,
test ID and the field name followed by `expected boolean`. Both JSON and Markdown
commands exit 2 with empty stdout and unchanged DB bytes; the library raises
`LedgerError`. This applies to both adapters and full/archived evidence. Valid
booleans remain unchanged, and upstream gaps remain independent of archive
removal and retained available counts. For example, stored `reported_flaky: 1`
is rejected when selected, while reporting only another valid run still works.
These are type checks, without source replay or inference of flag consistency.

Selected `reported_duration_ms` must be present and contain a finite nonnegative
JSON number, excluding booleans, null, strings, arrays and objects. Native values
sum reported attempt durations and may exceed `2^53-1` while finite; CTRF values
are final-test durations and keep the adapter's per-test `2^53-1` ceiling. Zero
and fractional milliseconds are valid. The same validator serves comparison,
history and JUnit. For example, stored `reported_duration_ms: true` or `-1` now
raises `LedgerError` with run ID, test ID, field name and `expected finite
nonnegative number`; JSON/Markdown commands exit 2 with empty stdout and unchanged
DB bytes. Invalid nonfinite JSON tokens fail earlier at the observation decoder
with run/test context. Unselected malformed durations remain ignored, and unknown
run IDs fail before evidence decoding. These type checks do not compare against
source reports or assert equality with `available_attempt_duration_ms`: reported
and available attempt durations remain separate evidence, including after archive
removal and with upstream retry gaps.

Every selected local attempt's `duration_ms` must also be present and contain a
finite nonnegative JSON number at most `2^53-1`, excluding booleans. This matches
the comparison/history/JUnit per-attempt boundary for native and CTRF evidence.
Missing/null/string/container values, negatives and larger numbers fail before
classification or aggregation. Malformed attempt JSON, duplicate keys, nonfinite
numbers and excessive nesting also fail explicitly. Errors identify the run ID,
test ID, relational attempt index and `duration_ms`; the library raises
`LedgerError`, and JSON/Markdown commands exit 2 with empty stdout and unchanged
DB bytes. For example, a selected stored attempt with `duration_ms: true` or `-1`
is rejected; selecting another valid run still succeeds. Unknown IDs take
precedence over decoding. Zero, fractional and maximum durations retain their
numeric types. Native available-attempt sums may exceed the per-attempt ceiling;
reported durations remain independent. Archived retained summaries require no
local attempts or external archive reads, and upstream gaps remain gaps. This
checks JSON duration shape, without comparing SQL duration columns, replaying
sources or validating other full-attempt fields.

Every selected local attempt's `status` must be a present string in the
adapter's normalized set: native Playwright accepts `passed`, `failed`,
`timedOut`, `skipped` and `interrupted`; CTRF also accepts `pending` and `other`.
These are the same sets used by comparison, history and JUnit, preserving both
mapped CTRF and raw Playwright statuses. Missing/null/boolean/numeric/container
values, blank strings and unsupported strings fail before classifying that
observation, including when the corrupt status belongs to an earlier retry.
For example, stored `status: "unsupported-status"` raises `LedgerError` with run
ID, test ID, relational attempt index and `.status`; JSON/Markdown exit 2 with
explicit stderr, empty stdout and unchanged DB bytes. Unknown IDs fail before
decoding, and unselected malformed statuses are ignored. `not_run` remains a
classification for no-attempt evidence, not a valid local attempt status.
Valid genuine/mapped fail-then-pass, expected failures, timeouts, interruptions,
archived retained classifications and upstream gaps retain their meaning. This
checks JSON status shape without comparing relational status columns, replaying
sources, opening stored paths or auditing other fields.

Archived observations must have a `retained_attempts` object with exactly
`available_attempts`, `available_attempt_duration_ms` and `classification`.
Counts must be nonnegative integers (booleans excluded, at most `2^53-1`);
durations must be finite nonnegative numbers (booleans excluded). Classifications
must contain a supported native/CTRF terminal status (or `not_run`) and every
existing boolean outcome
flag, with their correct types and no extra fields. Missing/empty/malformed
retained summaries fail rather than treating archive removal as a test that did
not run. Full observations cannot claim retained summaries; archived observations
cannot also contain local attempt rows. Observation JSON rejects duplicate keys,
nonfinite numbers and excessive nesting. These checks apply only to selected
observations, with run/test context and field diagnostics for shape errors, and
the same exit-2/empty-stdout contract for both JSON and Markdown. For example, changing an archived
`available_attempts` from `1` to `true` fails explicitly; selecting only an
uncorrupted full run still succeeds. Valid zero-attempt archived observations
remain supported, and valid mixed-selection output stays unchanged.

These shape checks do not prove that well-typed retained values match their
original source; use explicit `verify-archive` for that audit. Selected results
remain in memory and run membership is scanned; no streaming or large-history
performance claim is made.

## Export one run as JUnit XML

Export an exact known run for a CI system that accepts JUnit XML:

```bash
qawrench flaky report --db /tmp/flaky-ledger.sqlite --format junit --run-id pw-1 > retained-pw-1.xml
```

This exports **retained observations**, without re-executing tests. Exactly one
`--run-id` flag is required; repeated flags (including identical IDs), blank or
unknown IDs fail with exit 2 and empty stdout. JSON/Markdown selection and output
remain unchanged. Library equivalent:

```python
from qawrench.flaky import junit_report
xml_text = junit_report("history.sqlite", run_id="pw-1")
```

The deterministic UTF-8 XML has one `testsuites` root, one `testsuite`, and one
`testcase` per retained identity, ordered by fingerprint. A zero-observation run
has an empty suite with zero counts. `classname` is the canonical JSON tuple
`[adapter, project, project_id, file]`; `name` is the canonical JSON title path.
These preserve adapter/project/file/title boundaries without delimiter guessing.
The fingerprint and identity fields are also testcase properties. XML markup is
escaped; XML 1.0 forbidden characters cause an explicit error without partial
output or lossy replacement.

| Recorded terminal evidence | JUnit result |
| --- | --- |
| Known pass, including pass after retry | Passing testcase; retry/flakiness evidence stays in properties |
| Failed, including CTRF mapped failure | `failure` |
| Timed out or interrupted | `error` |
| Unexpected pass against native expected status | `failure` with `unexpected_pass` type |
| Known expected failure | `skipped` with `expected_failure` type; never represented as an observed pass |
| Recorded skip | `skipped` |
| No available observation, not-run, pending, other, or ambiguous terminal evidence | `skipped` with `unavailable` type |

Full evidence must retain the final attempt index indicated by `retries`; a
remaining earlier pass cannot establish a terminal pass if that final row is
missing. Archived evidence uses its retained terminal classification and
available count. No attempt testcases, absent test identities, global-error
testcases, or source error payloads are invented. Outcome messages explain the
normalized state; arbitrary producer logs/messages are not exported.

Properties preserve reported/expected/terminal statuses, reported flakiness,
native and mapped fail-then-pass, retries, available count/duration, reported
duration, `terminal_observation_available`, `attempts_archived`, and independent
upstream `attempt_details_unavailable` / `status_detail_unavailable` flags.
Missing prior retries do not hide a known final pass. Testcase `time` converts
only finite stored **available attempt milliseconds** to decimal seconds,
including retry time; it is omitted when the available count is zero. It is
neither wall time nor an estimate of unavailable attempts. Suite/root counts
count testcases, including skipped/unavailable cases, rather than attempts.

The exporter reuses the strict selected identity/evidence validators shared by
comparison/history and report JSON/retained validation, in one read-only v1/v2
snapshot. Malformed selected fields fail with contextual stderr, exit 2, and
empty stdout; unselected corrupt evidence stays uninspected. Source bytes,
metadata, digests and archive-reference contents are unused; no archive is
opened, source replay performed, schema changed, or data written. This is not a
replay audit or proof of complete inventory or historically accurate evidence;
use `verify-run` / explicit `verify-archive` for an audit. Output is buffered in
memory. CI consumers differ in their support for testcase properties; preserve
the XML artifact if the consumer does not display them.

## Compare two exact runs

Discover IDs with `list-runs`, then compare stored observations:

```bash
qawrench flaky compare-runs --db /tmp/flaky-ledger.sqlite --base-run-id pw-1 --run-id pw-2
```

The library entry point is `compare_runs(db_path, *, base_run_id, run_id)`.
Both IDs are required, literal, case-sensitive, nonblank UTF-8 strings; they may
be identical. No trimming, patterns or latest-run inference is performed.
Output is deterministic JSON with `action: "compared"`, both exact IDs,
`base`/`current` run descriptors (`format`, `project_context`, `state`,
`observation_count`) and a `tests` array sorted by stable `test_id`. Each row
names its format, project name/ID, exact file and title path, opaque-path flag,
and `membership: "base_only" | "current_only" | "both"`.
Its `base` and `current` sides are evidence objects or `null` when absent.
**Absence means no stored observation, never an inferred unexecuted test.**
Adapters, projects, exact files and titles remain separate identity namespaces;
there is no cross-format, rename or hierarchy matching.

Each evidence side contains `expected_status`, `reported_status`,
`reported_flaky`, `retries`, `outcome` (the existing terminal status and boolean
classification flags), `available_attempt_count`, `available_attempt_duration_ms`,
`reported_duration_ms`, `attempts_archived`, `attempt_details_unavailable` and
`status_detail_unavailable`. Available counts describe historical available
evidence, including retained counts after pruning. `attempts_archived` means
local detail was removed; `attempt_details_unavailable` means an upstream gap.
They are independent: an archived CTRF aggregate with two retries still has one
available attempt and an upstream gap. Archive removal never invents `not_run`.

Concrete synthetic cases (covered by `test_compare_runs.py`): native `shared`
changes from failed→passed (2 attempts, duration 5ms, retries 1) to passed
(1 attempt, 7ms, retries 0) with `both` membership. A stored native `absent`
observation with no attempts is `base_only`, count 0, duration 0 and `not_run`;
a newly named current observation is `current_only`, with a null base side.
After pruning, the native base keeps those counts/outcomes and adds the archive
marker. Full versus archived CTRF comparisons preserve aggregate gaps and
mapped fail-then-pass separately. Comparing the shipped native/CTRF examples
gives 14 identities with no `both` rows, even for similar names. Two selected
zero-observation runs return `tests: []` with zero counts; an empty run against
a nonempty run yields only one-sided rows. Selecting the same ID twice yields
`both` rows with identical evidence sides.

Membership and both evidence sets share **one read-only v1/v2 snapshot**.
Unknown/invalid IDs and malformed selected identity/context or metrics fail with
`LedgerError`, CLI **exit 2, explicit stderr and empty stdout**, without changing
DB bytes. Membership is checked before selected evidence decoding. Examples:
`--run-id PW-1` fails if only `pw-1` exists; a blank ID fails; boolean retries,
negative/nonfinite durations, malformed classification flags or boolean retained
counts fail with run/test/field context. Selected observation/attempt JSON also
rejects duplicate keys and excessive nesting. Full attempt index/status/duration
must agree with their relational columns. Counts/indices use nonnegative safe
integers; duration sums may exceed the per-attempt limit if still finite.
Unselected corrupted evidence cannot block the comparison.

Only selected normalized identity/metrics and full-attempt fields used for the
comparison are checked. Source blobs, run metadata, digests and archive-reference
contents are not read or audited; archive state comes from the stored marker.
No stored archive/attachment path is opened, no source is replayed, no schema is
changed and no data is restored. This does not prove complete observations,
producer authenticity, well-typed values' historical accuracy or consistent
classification flags; use `verify-run`/`verify-archive` for replay audits.
Results remain in memory; no performance or broad concurrency guarantee is made.

## One exact test across selected runs

```bash
qawrench flaky test-history --db /tmp/flaky-ledger.sqlite --test-id LOWERCASE_SHA256 --run-id pw-1 --run-id pw-2 --run-id pw-3
```

Library: `test_history(db_path, *, test_id, run_ids)`. Supply the existing stable
`test_id` fingerprint from `report` or `compare-runs`: exactly 64 lowercase hex
characters, encoding the adapter/project/project-ID/file/ordered-title identity
described below. There is no fuzzy name, rename or cross-format matching.
`run_ids` is a nonempty iterable of exact nonblank UTF-8 strings; scalar strings
and bytes are rejected. CLI requires at least one `--run-id`. Order and repeated
IDs are preserved literally, including case and surrounding spaces.

The deterministic JSON object contains `action: "test-history"`, `test_id`,
`identity` (the same namespace fields as comparison, or null if absent in every
selected run), and `runs` in caller order. Each run has `run_id`, `format`,
`project_context`, `state: full|archived`, `presence: present|absent`, and
`evidence`. Present evidence uses the comparison fields: stored outcomes,
expected/reported status, retries/flakiness, reported duration, available attempt
count/duration, `attempts_archived`, `attempt_details_unavailable` and
`status_detail_unavailable`. Absent evidence is null: neither a pass nor an
inferred unexecuted test. An observed native test with no attempts is instead
present, has available count 0 and outcome `not_run`. Full/archived states apply
even to zero-observation runs. Archive removal and upstream gaps are independent.

Concrete three-run contract: a native identity failed twice (durations 2+3ms,
retries 1) in `Fail-A`, failed once (4ms) in `fail-b`, then passed once (7ms) in
`Pass`. Selecting `Pass, Fail-A, Pass, fail-b` returns those four records in that
order, with available counts `1,2,1,1`. Pruning the failures preserves their
counts/outcomes and marks archived attempts. A selected zero-observation run
returns absent/null. CTRF aggregate retries retain a count of 1 with retries 2
and an upstream gap, including when archived; mapped outcomes stay distinct.

Run membership and the fingerprint's existence anywhere in observation keys are
checked before decoding evidence. Unknown runs or an unknown fingerprint fail;
a known fingerprint with no selected observations succeeds with null identity
and absent records, without decoding its unselected observations. One read-only
v1/v2 SQLite snapshot reads only selected run context and the exact selected
identity's observation/attempt evidence. Other identities in those same runs and
all other runs' evidence remain uninspected. Malformed relevant identities,
context, metrics, JSON or attempt-column disagreements raise `LedgerError`;
CLI errors have **exit 2, explicit stderr and empty stdout**, leaving DB bytes
unchanged. Invalid IDs fail before opening SQLite. No source replay, raw source,
metadata/digest/archive-reference-content reads, external paths, schema changes
or archive restoration. Shared comparison validation checks shapes, not source
authenticity, complete inventories, orphan rows, historical accuracy or internal
classification consistency. Results remain in memory; no performance guarantee.

## Supported input dialects and loss accounting

Compatibility is limited to synthetic fixtures checked against these pinned
upstream serializers; this is not a claim of testing every report their releases
can produce. New/unknown dialects fail explicitly where their version/shape can
be identified. Neither format reliably identifies the reporter implementation
version; a matching envelope cannot prove its producer.

* **Native Playwright 1.56.1 JSON**: requires `config.version == "1.56.1"`,
  `config.projects` with `id` and `name`, nested `suites[].specs[].tests[].results`,
  `errors`, and final `stats`. Suites require `title`, `file`, `specs`; specs
  require `title`, `file`, nonempty `tests`. Each test requires `projectName`,
  `projectId`, `expectedStatus`, `status`, `results`. Each result requires
  `status`, finite nonnegative `duration` (ms), and contiguous zero-based `retry`.
  Empty results are accepted only with outcome `skipped`; this is `not_run`,
  not an invented skipped attempt. Interrupted results are accepted even though
  Playwright may classify the overall outcome as `skipped`.
* **Playwright CTRF reporter 0.0.29 comprehensive legacy dialect**: requires
  `results.tool.name == "playwright"`, summary test/status counts and `start`/
  `stop`, and `results.tests` with `name`, `filePath` (capital P as emitted by
  source), string `suite`, `status`, `duration`, integer `retries`, boolean
  `flaky`. `rawStatus` and `retryAttempts` are optional. When present,
  `reportFormat` must be `CTRF` and `specVersion` must be `0.0.0`.
  `retryAttempts` contains **earlier** attempts; `tests[].status/duration` is the
  final observation. A present retry array must match `retries` exactly; absent
  details with nonzero retries set `attempt_details_unavailable: true`.
  Minimal reports that omit identity metadata, lowercase `filepath`, newer
  array-valued suites, other CTRF producers, and other declared spec versions
  are rejected. No silent field substitution.

[Native JSON types](https://github.com/microsoft/playwright/blob/v1.56.1/packages/playwright/types/testReporter.d.ts)
and [serializer](https://github.com/microsoft/playwright/blob/v1.56.1/packages/playwright/src/reporters/json.ts)
define the native shape. The [CTRF serializer](https://github.com/ctrf-io/playwright-ctrf-json-reporter/blob/v0.0.29/src/generate-report.ts)
clarifies differences from its README: `filePath`, string suite, prior-only retry
arrays, and raw-status-based summary counters. Expected failures may have
`test.status = "passed"`, `rawStatus = "failed"`, and increase `summary.failed`;
the adapter checks those actual counter semantics instead of rejecting them.

Until explicitly pruned, every accepted input is stored byte-for-byte in
`runs.raw_report`. Pruned source bytes and full attempts live in the separate
archive bundle described above. Ancillary
steps, stdout/stderr, annotations, errors, attachments and unknown metadata are
retained there; test/attempt source objects are also stored. Only test-level
statuses/durations are normalized; step timings and external attachment contents
are not analyzed or opened. The summary surfaces:

* `attempt_details_unavailable`: missing prior retry details, or native tests
  with no results. Available final observations remain available. No index gaps
  are filled. `attempt_duration_complete` is false if this count is nonzero.
* `status_detail_unavailable`: CTRF prior attempts have mapped statuses or the
  final raw status is absent. CTRF maps interruptions/timeouts to `failed`.
  Therefore `mapped_fail_then_pass` is separate from native `fail_then_pass`.
* `reported_flaky` versus `fail_then_pass`: the reporter's flaky label alone
  does not prove an observed failure followed by a pass. Native unusual flaky
  sequences, such as fail-then-skip, retain their label without being counted as
  fail-then-pass. `recurring_fail_then_pass` means such evidence in at least two
  independent run IDs; `recurring_reported_flaky` is separately available.
* `terminal_failure`: final failed/timedOut status excluding known expected
  failures. `expected_failure`, `unexpected_pass`, `skipped`, `interrupted`,
  `not_run`, and `terminal_status` keep other outcomes distinct. Final CTRF
  mapped failures retain the status-detail flag when raw status is absent.
* `available_attempt_duration_ms`: sum of **only available** attempt durations.
  `reported_test_duration_ms` sums native attempt durations but CTRF final-test
  durations; it is not an interchangeable cross-format total. `wall_duration_ms`
  comes from native stats or CTRF stop minus start; parallel test durations need
  not match it. `retry_incidence` is retried observations / all observations,
  including skipped/unexecuted observations in that denominator.
* `run_completion_unavailable`: these inputs do not give a dependable full-run
  completion status. Native global errors are retained and counted; CTRF can
  omit unexecuted tests, so we cannot reconstruct the discovered test inventory.

## Identity, transactions and failures

The stable identity is SHA-256 of a canonical JSON tuple containing adapter
namespace, project name, native project ID (null for CTRF), exact file string,
and ordered title path. Native title paths exclude the outer file suite and
include every nested suite plus spec title. CTRF title paths are `[suite, name]`;
`suite` stays opaque and is flagged `identity_path_opaque`. These choices avoid
lossy delimiter splitting and merging identical titles in different projects or
files. No line numbers, transient upstream test IDs, or checkout root are keys.
File strings are not rewritten: CTRF often uses absolute paths, which must be
kept stable by the caller. Adapter namespaces prevent accidental cross-format
merges. Importing the same physical run in two formats under different IDs counts
it twice; the tool cannot recognize that duplication.

Duplicate identities within a report, including repeatEach/merged-shard
collisions, are rejected instead of collapsed. Cross-format identity matching,
cross-renames, newer schemas, and mixed-project legacy CTRF imports are outside
this version's supported scope.

The entire report is validated before a writable database is opened. Imports
use `BEGIN IMMEDIATE`, foreign keys, and a single transaction for run, test and
attempt rows, including first-use schema creation. A conflicting run or SQLite
write failure rolls back all its changes. An existing foreign/unsupported-version
SQLite database is rejected, never migrated or replaced. The explicit `prune`
command alone upgrades supported v1 storage to v2 when it archives full runs. A newly opened database
may remain as an empty file if SQLite initialization fails. Keep the database on
a local filesystem with normal SQLite locking support. Inventory snapshot tests
include a local WAL writer; broad concurrency and crash behavior are untested. Input reports and summary
histories are loaded into memory, so this version targets modest CI artifacts.

Bad JSON/UTF-8, duplicate JSON keys, nonfinite/negative/invalid durations,
unsupported schema/status, missing required fields, noncontiguous native retry
indices, inconsistent retry arrays or summary counts, and duplicate identities
produce explicit stderr diagnostics and exit 2. Known numeric fields must be
finite, nonnegative and at most `2^53 - 1`; retry indices/counts must be integers. Summary-count checks catch
structurally partial reports; plausible-but-truncated source data with rewritten
matching counts cannot be proven incomplete. JSON/Markdown report and export
open SQLite read-only; reporting a missing path does not create a file.

Library use:

```python
from pathlib import Path
from qawrench.flaky import import_report, report

result = import_report("history.sqlite", Path("examples/flaky/playwright.json").read_bytes(),
                       format="playwright", run_id="pw-1")
summary = report("history.sqlite")
assert summary["totals"]["fail_then_pass"] == 1
```

Transfer library API:

```python
from qawrench.flaky import export_ledger, import_ledger

bundle_bytes = export_ledger("history.sqlite")
result = import_ledger("combined.sqlite", bundle_bytes)
assert import_ledger("combined.sqlite", bundle_bytes)["imported_runs"] == 0

selected_bytes = export_ledger("history.sqlite", run_ids=["pw-2", "pw-1", "pw-2"])
empty_bytes = export_ledger("history.sqlite", run_ids=[])
```

`run_ids=None` (the default) exports all runs; an iterable of nonblank UTF-8
encodable strings exports those exact IDs once each in canonical order. An empty
iterable exports a valid empty bundle. Scalar strings/bytes, invalid IDs and
unknown IDs raise `LedgerError`. Membership and source checks share a read-only
SQLite snapshot; only selected runs' source digests/archive state are checked.

Schema v1 (and v2 after pruning) has `runs` (source bytes/metadata), `observations` (one row per stable
test/run), and `attempts` (available observations with original indices/statuses/
durations/source objects). V2 adds archive references and retained observation
metrics in place of archived full attempts. Reports include per-run evidence and per-test history;
there is no hosted dashboard, browser execution, source blame or inference of
missing attempts. Report files may contain sensitive logs from their producer;
this program only handles the local files supplied to it.
## Tests

From a qawrench checkout:

```bash
uv run --extra test pytest tests/flaky -q
```


Tests run sequentially with temporary SQLite databases and subprocess CLI calls.
They cover both source-checked report shapes, deeply nested suites, projects,
expected failures/unexpected passes, interruption/unexecuted tests, aggregate and
detailed CTRF retries, recurring identities, raw-byte recovery, idempotency,
conflicting-run rollback, a storage failure after writes have started, invalid
reports, and read-only/dry-run behavior. Bundle tests additionally cover
determinism across insertion orders/timestamps, two-worker merges in either
order, exact-byte/evidence roundtrips, duplicate IDs, malformed envelopes/source
reports, and conflicts or storage failures after earlier runs were written.
Selected-export regressions cover repeatable CLI flags, canonical ordering,
deduplication, Unicode/literal IDs, exact source and bundle roundtrips, empty
library selection, unknown/invalid IDs with empty CLI stdout, and selected full
runs in a pruned ledger.
Retention tests additionally prove unchanged totals/recurrence, exact archive recovery,
read-only planning, no-clobber publication, file/directory sync failures, late SQL
rollback including schema upgrade, deterministic timestamp ties, repeat pruning,
and v2 imports. Archive-verification tests check both adapters, relocated
bundles, zero-observation runs, exact membership, every retained observation
field/classification and metadata field, digest/context tampering, malformed
sources/retained JSON, archived-detail invariants and empty-stdout read-only
failures. Full-run verification tests additionally check exact native/CTRF
source replay, every metadata/observation/attempt field, SQL attempt columns
versus JSON, orphan/missing/extra attempts, exact selection, zero-observation
runs, fractional durations and archived-run guidance without stored-path reads.
Archive-inventory tests cover multiple native/CTRF prune groups, sorted opaque
paths, zero-observation runs, v1 empty output, malformed references/counts with
empty stdout, unchanged bytes, excluded full evidence and a concurrent WAL writer
proving references and counts share one snapshot. Run-inventory tests additionally
cover full/archived filters, exact case-sensitive IDs, v1 empty/native/CTRF runs,
zero-observation runs in both states, selected context/reference/count errors,
ignored unselected evidence and a WAL writer changing counts and references.
Selected-report tests cover JSON/Markdown and library selections, exact literal
IDs, deduplication, selected recurrence/totals, full/archived upstream gaps,
empty selections, invalid/unknown IDs, byte-preserving failures, unselected
invalid UTF-8 metadata and a local WAL writer proving snapshot consistency.
They also reject malformed/missing retained summaries, invalid count/duration/
classification types, conflicting full/archived evidence, duplicate observation
keys and excessive nesting. Positive boundaries preserve CTRF pending/other,
zero-attempt archived observations and valid duration sums above `2^53-1`.
Comparison tests additionally cover exact sorted identity membership and null
absence, changed native outcomes, zero available/zero-observation cases, full and
archived native/CTRF evidence, preserved aggregate gaps, project/file/title and
adapter separation, malformed selected metrics/JSON/identity/context,
relational attempt mismatches, ignored unselected corruption and one WAL snapshot.
Fixtures are synthetic; no real-site improvement, adoption, market demand or
performance gain has been measured.

## License

Original implementation: [MIT](../LICENSE). See [THIRD_PARTY.md](../THIRD_PARTY.md)
for upstream format references and license boundaries.
