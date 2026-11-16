# qawrench lighthouse

`qawrench lighthouse` (formerly `lighthouse-noise-gate`) compares baseline and current
Lighthouse numeric audits across repeated, comparable runs. It reports every selected
run value, medians, raw median absolute deviation (MAD), deltas, and explicit
**pass / regression / inconclusive / invalid** decisions. No runtime dependencies,
Chrome, server, network access, or script execution.

[Lighthouse CI already repeats runs and aggregates assertions](https://github.com/GoogleChrome/lighthouse-ci/blob/v0.15.1/docs/configuration.md).
Its aggregation methods include median, median-run, optimistic and pessimistic.
This tool adds an independent baseline-versus-current numeric-value policy with
visible spread, strict comparable groups, and abstention when noise overlaps a
tolerance. It makes no claim to invent median aggregation or to replace LHCI.
[Lighthouse's variability guide](https://github.com/GoogleChrome/lighthouse/blob/v12.8.2/docs/variability.md)
explains why repeated observations still need care.

## Install

Requires Python 3.11 or newer.

```sh
pipx install qawrench               # or: pip install qawrench
qawrench lighthouse --version       # prints the bare qawrench version

# Or run without installing:
uvx qawrench lighthouse --help
```

`python -m qawrench lighthouse` is equivalent to `qawrench lighthouse`. This command
has no third-party runtime dependencies.

The synthetic inputs below live in `examples/lighthouse/` in the qawrench repository;
run the commands from the repository root (or adjust the paths).

## Working examples

```sh
qawrench lighthouse examples/lighthouse/baseline examples/lighthouse/current \
  --config examples/lighthouse/gate.json --out /tmp/lighthouse-report.json \
  --markdown /tmp/lighthouse-report.md

# Without output paths, emits complete JSON on stdout:
qawrench lighthouse examples/lighthouse/baseline examples/lighthouse/current \
  --config examples/lighthouse/gate.json

# Computes the same decision and prints JSON, with no output-file writes:
qawrench lighthouse examples/lighthouse/baseline examples/lighthouse/current \
  --config examples/lighthouse/gate.json --dry-run
```

The included **synthetic** baseline LCP median is 2000 ms (MAD 10 ms); current
median is 2010 ms (MAD 10 ms), despite one 9000 ms outlier. Tolerance is 100 ms,
noise margin is 20 ms, adverse delta is 10 ms: **pass, exit 0**. The outlier
remains visible. These are independently authored LHR projections, not measured
pages or full reports for Lighthouse's HTML renderer.

Directories are scanned for immediate `*.json` files in filename order, without
recursion. Every matching file must be an LHR: separate LHCI `lhr-*.json` files
from manifests, assertion results, HTML and other metadata before comparison.
Multiple URLs/devices can share a directory if **every exact group has a
counterpart** on the other side. Runs from different groups are never pooled.
Keep output files outside both input directories. Output parents must already
exist; JSON and Markdown paths must differ and cannot overwrite the config.
Explicit report paths are atomically replaced individually, not as a two-file
transaction. If the second write fails, the first report may already exist.

For a concise Markdown report, add `--markdown-summary` with `--markdown`:

```sh
qawrench lighthouse examples/lighthouse/baseline examples/lighthouse/current \
  --config examples/lighthouse/gate.json --out /tmp/lighthouse-report.json \
  --markdown /tmp/lighthouse-summary.md --markdown-summary
```

Summary mode retains the group, metric and confidence decision tables and all
emitted mismatch rows and truncation counts. It replaces the `Complete evidence`
JSON block with input diagnostics, metric decision reasons, requested confidence
diagnostic reasons, retained MAD evidence when the confidence gate is active,
stored relative deltas and their reasons, per-side confidence interval/rank evidence,
MAD heuristic operands, and a note to
use the JSON report for full evidence.
JSON output and gate decisions are unchanged; without this option, Markdown
remains complete and byte-identical. If `--out` is absent, the complete JSON
report is printed to stdout as usual. `--markdown-summary` requires `--markdown`
even with `--dry-run`; dry-run still prints complete JSON and writes nothing.
Invalid options are rejected before either report is replaced.

Concise input diagnostics state exact **report error** and **run warning** counts,
including zero. Errors are the existing `report.errors` entries in stored order,
with their original source prefixes (such as `current/broken.json`,
`current/run-0.json`, or `group <id>`). Run/group copies of those errors are not
counted again. A malformed current LHR and one known-profile invalid selected
metric produce two report errors when all profiles otherwise match. Every error
message is listed, so an invalid result can be diagnosed without opening JSON.

Warnings list side, filename and message in baseline/current order, then stored
run and warning order. Each `runWarnings` array entry counts once, including
duplicates; absent or empty arrays count zero. Warning-only input can still pass.
The comparison preserves unvalidated warning payloads: a non-array payload,
including `null`, is displayed as one raw value; non-string entries use JSON
notation. Rendering does not classify these payloads as new input errors.
Pipes are escaped and line breaks flattened in diagnostic table cells. These
tables appear only in concise mode; complete Markdown retains its original
embedded evidence. Counts do not include missing-value reasons or inferred errors.

Concise **Metric decision reasons** identify the group and audit for every stored
`metric.reasons` entry. The occurrence count includes duplicates and empty
messages; group, metric and reason order are preserved. Zero is stated explicitly,
with no reason table or invented placeholder when all arrays are empty (or there
are no groups). All cells escape pipes and flatten line breaks. Reasons include
missing/null values even when enough other samples remain, insufficient samples,
noise overlap, invalid input, passes and regressions.

These are the **active gate's** existing reasons. With diagnostic-only confidence,
they remain MAD reasons; with `--confidence-gate`, they reflect the confidence
gate, including its larger sample requirement. The renderer does not append
`mad_reasons`, separate `confidence.reasons`, `relative_delta_reason` or inferred
messages to the active reason rows. Relative-delta values and reasons appear in their own concise section below.

When confidence was requested, concise **Confidence diagnostic reasons** separately
lists only stored `metric.confidence.reasons`, labeled by group and audit. Each
occurrence counts once, including duplicates and empty messages, in stored group,
metric and reason order. All cells escape pipes and flatten line breaks. Zero is
explicit, with no table or placeholder rows, including when there are no groups.
Without a confidence request this section is absent. It does not infer reasons
from interval status or active decisions; available intervals normally have no
diagnostic reasons, even when the confidence gate is inconclusive.

For example, the five-sample fixture with `--confidence-level .95` still has a MAD
**pass, exit 0**. Its separate confidence diagnostic is inconclusive and explains
that at least seven complete valid samples per side are required. Missing metrics
and invalid-input interval suppression retain their existing diagnostic reasons.
With `--confidence-gate`, a reason can appear in both separately labeled sections
if it exists in both source arrays; it is not deduplicated across sections. Active
metric reasons, confidence summaries, input diagnostics and truncation tables
remain intact.

With an active `--confidence-level .95 --confidence-gate`, concise **Retained MAD
evidence** separately lists each stored `metric.mad_decision` and every stored
`metric.mad_reasons` entry, labeled by group and audit. Decision occurrences count
metrics, even when their reason arrays are empty; reason occurrences include
duplicates and empty messages. Both counts state zero explicitly, including when
there are no groups. Zero decisions or reasons produce no corresponding table or
placeholder rows. Stored group, metric and reason order is preserved, and all
cells escape pipes and flatten line breaks. These are retained heuristic results;
the active confidence gate still determines the decision and exit code.

For example, seven baseline values of 2000 ms versus six current values of
2050 ms and one of 2300 ms, with a 100 ms absolute tolerance and zero relative
tolerance, retain a MAD **pass** while the confidence gate is **inconclusive**
(adverse interval [50, 300] ms). One current value of 2050 ms and six of 2200 ms
instead retain a MAD **regression**, also confidence-inconclusive ([50, 200] ms).
Both stored decisions and their MAD reasons are visible without opening JSON.
Agreements and invalid-input MAD evidence are retained too. The renderer uses
only those stored fields, with no fallback to active decisions, interval-derived
explanations, or other reason arrays. Without an active confidence gate (including
diagnostic-only confidence), this section is absent. Active metric reasons and
confidence diagnostic sections are unchanged; default complete Markdown and JSON
retain their original bytes.

Concise **Relative deltas** lists each stored `metric.relative_delta` and
`metric.relative_delta_reason`, labeled by group and audit in stored order.
Values are raw ratios (for example `0.1` or `-0.1`), with no percentage conversion,
rounding or median recomputation. Actual numeric zero stays zero; a null value or
reason is shown as `null`, while an empty stored reason stays blank. Every cell
escapes pipes and flattens line breaks. The row count includes every metric,
including those with null values. With no groups, the count is explicitly zero
and no table or placeholder rows are emitted.

Both zero-to-zero and zero-to-positive comparisons normally store a null ratio
and the explanation `baseline median is zero; relative delta undefined; absolute
tolerance applies`. Missing, invalid or overflowed statistics can also store a
null ratio with a null reason; the renderer does not invent an explanation from
null or reconstruct overflow diagnostics. Any existing stored reason is retained.
This section appears in concise reports in all confidence modes, separately from
active metric reasons, confidence diagnostics and retained MAD evidence. Gate
decisions, default complete Markdown and all JSON bytes are unchanged.

Concise **MAD heuristic operands** lists each stored `metric.adverse_delta`,
`metric.tolerance`, `metric.noise_margin` and `metric.regression_boundary`, labeled
by group and audit, with **Unit** and **Direction** columns from stored `metric.unit`
and `metric.direction`. For example, a report selecting LCP and a synthetic throughput
audit can show `millisecond` / `lower` and `unitless` / `higher` alongside their
respective operands. `lower` means lower is better; `higher` means higher is better.
The renderer reads these fields directly without recomputing
them from medians, direction or policy. Numbers retain their stored representation
without rounding, including actual zero; null is shown as `null`. The row count
includes every metric, including all-null rows, in stored group/metric order with
duplicates retained. All cells escape pipes and flatten line breaks. With no
groups, the count is explicitly zero and no table or placeholder rows appear.
Stored units and directions are shown literally, even if inconsistent with the
audit name or policy; the renderer does not infer context, convert units or validate
these fields again. Empty strings stay blank, and a stored null context is shown
as `None` by the existing cell formatter (numeric operand nulls remain `null`).
Missing required context or operand keys raise `KeyError` in concise library
rendering rather than substituting a guessed value.

For example, baseline values `[1990,1990,1990,2000,2010,2010,2010]` and current
values 120 ms higher, with absolute tolerance 100 ms, zero relative tolerance and
noise multiplier 2, store adverse delta `120.0`, tolerance `100`, noise margin
`20.0` and regression boundary `120.0`. The MAD result is inconclusive at this
boundary. For a higher-is-better audit, the same downward shift stores the same
adverse operands. Missing or overflowed statistics can leave all operands null;
partial missing or invalid input can retain numeric operands without establishing
a valid decision. Null never triggers an inferred value or explanation.

This section appears in all confidence modes. These are **MAD heuristic operands**,
including with active confidence; the MAD regression boundary is not the confidence
gate decision rule. The active confidence gate continues to use its stored interval
and absolute tolerance as described below. Existing active, confidence, retained
MAD, relative-delta, input and cap sections remain intact. Default complete
Markdown and all JSON bytes are unchanged.

When confidence is requested, concise **Confidence population-median evidence**
shows the stored `metric.confidence.baseline` and `current` **interval** and
**order_statistic_ranks**, labeled by group, audit and side. These are per-side
population-median intervals, separate from the existing median-change/adverse-change
intervals and MAD operands. For seven tied zero observations per side at level
`.95`, both rows show interval `[0, 0]` and ranks `[1, 7]`. Ten observations
`[0,1,2,3,4,5,6,7,8,9]` store interval `[1, 8]` and ranks `[2, 9]`.

Both diagnostic-only and active confidence include this table; without a confidence
request the section is absent. Every metric contributes two rows, baseline then
current, in stored group/metric order, including duplicates. The count includes
unavailable sides: a stored null side displays `null` for both fields. Individual
null fields also display `null`, including when inconsistent with status or decision.
Numbers and lists retain their stored representation, including zero; cells escape
pipes and flatten line breaks. No groups means an explicit zero count without a
table or placeholder rows. Missing required side or object field keys raise
`KeyError` in concise library rendering rather than deriving replacements.

The renderer never sorts run values, selects ranks again, recomputes endpoints or
infers availability from a decision. Insufficient, missing or invalid observations
and interval overflow retain their stored null evidence and existing diagnostic
reasons. Confidence coverage remains per group/metric under iid observations from
a fixed distribution within each side; no simultaneous or real-site guarantee is
implied. All existing sections, decisions, JSON bytes and default complete Markdown
remain unchanged.

The renderer also supports `markdown(report, include_evidence=False)` from
`qawrench.lighthouse.report`; the keyword accepts only a boolean and defaults
to `True`. Omitting embedded evidence avoids serializing it a second time in
Markdown. It does not bound input loading, comparison work, individual values,
the retained JSON evidence or the size of the remaining summary tables.

Library usage:

```python
from qawrench.lighthouse import compare, load_json

report = compare("examples/lighthouse/baseline", "examples/lighthouse/current", load_json("examples/lighthouse/gate.json"))
assert report["decision"] == "pass"
print(report["groups"][0]["metrics"][0]["current"]["mad"])
```

Bad policy or invalid saved manifest raises `ValueError`; directory-input data
failures appear in the report.
The library performs no output-file writes.

## Save and reuse a baseline

```sh
qawrench lighthouse save-baseline examples/lighthouse/baseline \
  --config examples/lighthouse/gate.json --out /tmp/lighthouse-baseline.json

qawrench lighthouse /tmp/lighthouse-baseline.json examples/lighthouse/current \
  --config examples/lighthouse/gate.json --out /tmp/lighthouse-saved-report.json
```

This example saves five runs and then **passes, exit 0**, with the same group
statistics as the directory comparison. The original baseline directory is no
longer needed. `save-baseline` prints the manifest to stdout if `--out` is
omitted; `--dry-run` validates and prints it without writing any output path.
Save the manifest outside the LHR directory: directory scans still treat every
immediate JSON file as an LHR. A directory literally named `save-baseline` can
be compared using `./save-baseline`.

```python
import json
from pathlib import Path
from qawrench.lighthouse import create_baseline, compare, load_json

policy = load_json("examples/lighthouse/gate.json")
manifest = create_baseline("examples/lighthouse/baseline", policy)  # no writes
Path("baseline.json").write_text(json.dumps(manifest), encoding="utf-8")
report = compare("baseline.json", "examples/lighthouse/current", policy)
```

The version-1 tool manifest is a self-contained JSON object with exactly
`kind: "lighthouse-noise-gate-baseline"`, `schema_version: 1`, `policy`, and
`runs`. Each run has `file`, `sha256`, and `lhr`: the original filename, SHA-256
of canonical JSON (sorted object keys, ASCII escaping, compact separators), and
the **full parsed LHR**, including unselected audits. It preserves the exact
URL/device/profile metadata and run evidence. No paths in a manifest are read
or executed. The format is this tool's own, distinct from an LHCI manifest.
Full snapshots can be large; loading and saving use memory proportional to
their contents. Checksums detect accidental content edits; they do not
authenticate the measurements or establish independent samples.

Creating or loading a saved baseline requires complete valid selected metrics
and at least the policy's `min_samples` **in each exact group**. A noisy baseline
can still be saved; saving does not assert a pass against a future current run.
Malformed/unknown schemas, duplicate run filenames, changed checksums, invalid
LHRs, and incomplete groups are rejected. The comparison policy must exactly
match the saved policy, ignoring object key order; even a changed tolerance or
numeric representation requires explicitly saving a new baseline. The library
raises `ValueError` for validation failures; the CLI exits 2 before replacing
any output. Neither save nor compare can overwrite its input files or config.

Groups remain independent. For example, with seven mobile and seven desktop
baseline LCP values of 2000 ms, current mobile values of 2500 ms regress by
500 ms under `examples/lighthouse/gate.json`. If current desktop changes its CPU slowdown
from 4 to 8, the overall result is **invalid, exit 2**, and the two unmatched
desktop profiles are invalid. Mobile still reports its regression and, with
`--confidence-level .95`, an available `[500, 500]` ms change interval. JSON
retains every group and run; Markdown lists URL, device, sample counts and
decision for each group before the metric details. Invalid known groups
suppress only their own confidence diagnostics. An unreadable run or invalid
metadata with no group identity still suppresses all confidence intervals,
because the affected group cannot be determined. Gate decisions for valid
groups remain visible in either case; the overall invalid exit never becomes
a partial pass.

## Profile mismatch diagnostics

Every comparison now includes a `mismatches` array (empty when there are no
candidate pairs). A baseline-only group and a current-only group are diagnostic
candidates only when **both `finalUrl` and `finalDisplayedUrl`, plus
`configSettings.formFactor`, match exactly**. URLs are not normalized. Already
matched groups and runs without valid group metadata are excluded. If several
profiles qualify, by default every baseline/current candidate combination is listed;
the tool does not infer a one-to-one correspondence or choose a nearest profile.
Records are sorted by baseline group ID, then current group ID.

For the desktop CPU change above, the record has this shape (IDs abbreviated):

```json
{
  "baseline_group_id": "baseline-id",
  "current_group_id": "current-id",
  "finalUrl": "https://synthetic.example.test/catalog",
  "finalDisplayedUrl": "https://synthetic.example.test/catalog",
  "formFactor": "desktop",
  "changes": [{
    "path": "/configSettings/throttling/cpuSlowdownMultiplier",
    "baseline_present": true, "baseline": 4,
    "current_present": true, "current": 8
  }]
}
```

`changes` lists differing config/browser fields in the projected group identity,
including unknown config keys and `userAgent`, `hostUserAgent`, and
`networkUserAgent`. Paths are JSON pointers relative to `identity`; `/` in a key
is escaped as `~1`, and `~` as `~0`. Object keys are traversed in lexical order.
Objects on both sides are diffed recursively. Lists are whole, order-sensitive
values; added/removed keys and object/scalar replacements retain the entire
value at that path. Presence flags distinguish missing keys from explicit
`null`; an absent side's value is encoded as `null`. Empty objects are preserved.
Equality follows group hashing: object key order is ignored, but JSON types
and numeric representations matter (`false` versus `0`, or `4` versus `4.0`).

These are explanations, not comparisons: samples remain separate, unmatched
groups/metrics remain **invalid**, and the overall result remains **exit 2**.
Matched groups keep their decisions and available confidence summaries in
either gate mode. Saved baselines produce the same diagnostics as directories.
Markdown adds a **Profile mismatches** table with both group IDs, changed paths,
and JSON values; `(missing)` distinguishes absence from `null`. Full identities
and all run evidence remain in JSON.

Use `--mismatch-limit 5` or `compare(..., mismatch_limit=5)` to bound diagnostic
candidate output. The limit must be a positive integer; the library rejects
booleans, floats and strings. Invalid values fail before replacing either report.
Omitting the option (or library `None`) preserves the existing unlimited JSON
and Markdown output. With an explicit limit, `mismatches` contains the first
N candidate pairs in the same global group-ID order. JSON additionally has these
global fields in `mismatch_summary` (plus the `groups` list described below):

```json
"mismatch_summary": {"limit": 5, "total": 12, "emitted": 5, "omitted": 7, "truncated": true}
```

Here three baseline-only and four current-only profiles at one exact
URL/formFactor anchor produce 12 possible pairs. Counts refer to **pairs**,
not changed fields; each emitted pair retains all its changes unless the
separate field limit below is supplied. With multiple
anchors, total is the sum of each anchor's baseline count times current count.
Unrelated anchors contribute no pairs. Markdown shows total/emitted/omitted,
the limit and whether output was truncated. At or above total, omitted is zero
and truncated is false; with no candidates, all three counts are zero and
truncated is false. A limit never changes group evidence, decisions, confidence
summaries or invalid exits, including for groups whose pairs were omitted.
Totals are counted without constructing the full Cartesian list; only emitted
pairs are diffed. This bounds candidate records only: all inputs, groups, run
evidence and individual changed values are still held in memory.

With an explicit limit, `mismatch_summary.groups` accounts for **every unmatched
group with valid identity**, sorted by `group_id`, including groups with no
eligible counterpart. Each entry has this shape (ID abbreviated):

```json
{"group_id": "unrelated-baseline-id", "side": "baseline", "total": 0,
 "emitted": 0, "omitted": 0, "truncated": false}
```

`side` is `baseline` or `current`. A group's `total` counts eligible unmatched
groups on the opposite side at its exact URL/URL/formFactor anchor. `emitted`
counts the emitted pairs containing this group; `omitted = total - emitted`,
and `truncated` is true exactly when `omitted > 0`. Per-side sums of these three
counts equal the global pair counts (summing both sides counts each pair twice).
For the 3-by-4 case, each baseline profile has total 4, each current profile has
total 3, and an unrelated group has total 0. A group can have positive total
with zero emitted pairs when the prefix ends before reaching it.

Markdown labels each group as `complete`, `truncated`, or
`no exact-anchor candidates`. These are candidate counts, never inferred matches
or valid comparisons. Already matched groups and runs with invalid identity
metadata have no entry. Valid identity with invalid metric evidence retains
its existing candidate eligibility and invalid decision. The accounting uses
anchor sizes and emitted endpoints, without enumerating omitted pairs. It is
available only with a limit; default unlimited JSON/Markdown remains unchanged.

Use `--mismatch-field-limit 5` or `compare(..., mismatch_field_limit=5)` to
bound the **change records per emitted candidate pair**. This independent option
works with or without `mismatch_limit`. It accepts a positive integer; library
booleans, floats and strings are rejected. Invalid options fail before replacing
either report. With no field limit (or library `None`), existing JSON and
Markdown output stays unchanged, including reports using the pair limit.

Each emitted pair gains `changes_summary` alongside its retained `changes`:

```json
"changes_summary": {"limit": 5, "total": 12, "emitted": 5, "omitted": 7, "truncated": true}
```

The first five records are retained in the existing depth-first traversal of
lexically sorted **raw object keys**. JSON-pointer escaping happens afterward;
this is not sorting the escaped pointer strings. Object/object differences
recurse; lists, added/removed whole objects and object/scalar replacements each
count as one record. Presence flags, missing/null and numeric/type differences
keep their existing meaning. At or above total, all changes remain with zero
omitted and `truncated: false`. No candidates means no per-pair summaries.
Markdown shows a separate table of per-pair change totals, emitted/omitted,
limit and truncation, before the retained field rows.

The traversal counts all changes while retaining only the requested prefix,
without building a full change-record list. Pairs omitted by `mismatch_limit`
are never diffed, including for field totals. Pair/global/per-group candidate
counts remain unchanged. These limits do not bound traversal work, individual
values, input size or the full identities and run evidence retained in JSON.
Groups stay invalid; no omitted changes, matches or comparisons are inferred.

To combine the options in the synthetic CPU example below, append
`--mismatch-limit 5 --mismatch-field-limit 5` to the comparison command. The one
CPU change has total/emitted/omitted `1/1/0` and is not truncated.

Reproduce a synthetic CPU mismatch with the installed CLI, from the repo root:

```sh
mismatch_dir="$(mktemp -d)"
python - "$mismatch_dir" <<'PY'
import json, shutil, sys
from pathlib import Path
root = Path(sys.argv[1])
for side in ("baseline", "current"):
    shutil.copytree(Path("examples/lighthouse/lighthouse") / side, root / side)
for path in (root / "current").glob("*.json"):
    doc = json.loads(path.read_text())
    doc["configSettings"]["throttling"]["cpuSlowdownMultiplier"] = 8
    path.write_text(json.dumps(doc))
PY
qawrench lighthouse "$mismatch_dir/baseline" "$mismatch_dir/current" \
  --config examples/lighthouse/gate.json --out "$mismatch_dir/report.json" \
  --markdown "$mismatch_dir/report.md"
# Expected: invalid, exit 2; one candidate pair, CPU baseline=4/current=8.
```

## Policy and decision boundaries

`examples/lighthouse/gate.json` is the complete version-1 config. All fields are required;
unknown fields are rejected, so a misspelled tolerance cannot silently pass.
Select one or more exact `audits` IDs. Each metric requires `direction` of
`lower` or `higher`, an exact `numericUnit` such as `millisecond` or `unitless`,
and finite, nonnegative `absolute_tolerance` and `relative_tolerance`.
`relative_tolerance: 0.05` means 5%. `min_samples` is an integer at least 3;
`noise_multiplier` is finite and nonnegative. Zero explicitly disables the
noise margin.

For each group/metric, let B and C be baseline/current medians:

```text
MAD(values) = median(abs(value - median(values)))       # unscaled MAD
Delta       = C - B                                    # signed input-unit change
Relative    = Delta / abs(B)                           # signed fraction, not %
D           = Delta for lower-better; -Delta for higher-better
T           = max(absolute_tolerance, abs(B) * relative_tolerance)
N           = noise_multiplier * max(baseline_MAD, current_MAD)

pass         if D + N <= T
regression   if D > T + N
inconclusive otherwise
```

Both samples must reach `min_samples` and have no missing selected metric before
these decision rules apply. Any missing/null audit or numericValue makes that
metric inconclusive, even when enough other observations exist; valid counts
and statistics remain visible. A missing value is never zero and `score` is
never a replacement. Baseline median zero produces JSON `null` for relative
delta with an explanation; the absolute tolerance still applies. Arithmetic
overflow is invalid and never leaks Infinity/NaN into a report.

MAD is a robust spread description. This noise band has no probabilistic
coverage, sample-size correction or confidence-interval interpretation. Zero
MAD can coexist with extreme outliers, so always inspect the full values.

## Optional repeated-run confidence summary

Add `--confidence-level 0.95` to request deterministic, distribution-free
intervals for the **population median change**, separately for each matched
group/metric. The library accepts the same option as a keyword:

```sh
qawrench lighthouse examples/lighthouse/baseline examples/lighthouse/current \
  --config examples/lighthouse/gate.json --confidence-level 0.95 \
  --out /tmp/lighthouse-confidence.json --markdown /tmp/lighthouse-confidence.md
```

```python
report = compare("examples/lighthouse/baseline", "examples/lighthouse/current",
                 load_json("examples/lighthouse/gate.json"), confidence_level=0.95)
print(report["groups"][0]["metrics"][0]["confidence"])
```

The existing five-run example still **passes the MAD gate, exit 0**, but its
confidence summary is **inconclusive**: a 95% median-change interval needs at
least **seven complete valid observations on each side**. These diagnostic
statuses and intervals do not alter gate decisions or exit codes unless the
confidence gate below is explicitly enabled. Omit both confidence options to
retain the original report format. A level outside `(0, 1)`, including
NaN or infinity, is a configuration error (library `ValueError`; CLI exit 2
before writing reports).

For level L, each side receives an equal-tailed median interval with target
coverage `(1 + L) / 2`. With n sorted observations and one-based rank k:

```text
interval = [X_(k), X_(n-k+1)]
coverage >= 1 - 2 * sum(comb(n, j), j=0..k-1) / 2**n
```

The implementation selects the largest k meeting the target using exact
integer/rational arithmetic. Even the widest finite interval (k=1, min/max)
needs `1 - 2**(1-n) >= (1 + L) / 2`. This gives statistical minima of 6, 7 and
9 runs per side for L = 90%, 95% and 99%. The effective minimum is the larger
of that requirement and the configured `min_samples`. More runs can shrink the
rank interval; the method does not resample or use an RNG.

Subtracting the baseline interval from the current interval gives
`delta_interval = [current_low - baseline_high, current_high - baseline_low]`.
The reported joint coverage lower bound is the sum of the two achieved
side coverages minus one (Bonferroni). `adverse_delta_interval` reverses and
negates the endpoints for higher-is-better metrics. The JSON retains the
selected ranks, floating coverage summaries and exact rational coverage
fractions; Markdown includes a confidence table and full JSON evidence.
Floating subtraction rounds endpoints outward when needed to avoid shrinking
the interval through rounding.

For a reproducible synthetic example, baseline
`[1980,1990,2000,2010,2020,2030,2040]` and current
`[2180,2190,2200,2210,2220,2230,2240]` yield side intervals `[1980,2040]` and
`[2180,2240]`, a change interval `[140,260]` ms, and coverage lower bound
`31/32 = 0.96875` when requesting 95%. The regression tests exercise these
Lighthouse 12.8.2 projections and enumerate all median-side sign patterns for
seven and ten observations to verify coverage and rank selection.

Coverage assumes **iid observations within each side from a fixed
distribution**. Matching Lighthouse metadata cannot establish independence or
stationarity; repeated copies of one run are not independent samples. Ties
give conservative coverage. Independence between baseline and current is not
required by the union bound. Coverage is per group/metric, with no correction
across multiple groups or metrics; it is neither a prediction interval for
future runs nor a false-positive guarantee for the MAD/tolerance gate.

Missing metrics suppress that metric's confidence intervals even if enough
other runs remain. Invalid groups suppress their own confidence intervals;
unassignable input errors suppress all intervals as described above.
Overflow in interval subtraction produces an `invalid` confidence diagnostic
with null endpoints and no NaN/Infinity, while preserving the MAD gate result.
All incomplete summaries retain reasons and sample evidence.

## Opt-in absolute-tolerance confidence gate

Use `--confidence-gate --confidence-level 0.95` to make the adverse-change
interval determine decisions. **Every selected metric must have
`relative_tolerance: 0`**; this version does not infer confidence bounds for a
tolerance that depends on the unknown population baseline median. The supplied
`examples/lighthouse/confidence-gate.json` uses an absolute LCP tolerance of 100 ms:

```sh
qawrench lighthouse examples/lighthouse/baseline examples/lighthouse/current \
  --config examples/lighthouse/confidence-gate.json --confidence-level 0.95 --confidence-gate
```

This five-run example is **inconclusive, exit 3**, because 95% requires at
least seven complete observations per side. Omitting `--confidence-gate`
retains the MAD **pass, exit 0** with a diagnostic-only confidence summary.
The library uses `compare(..., confidence_level=0.95, confidence_gate=True)`.

For an available adverse-change interval `[lo, hi]` and fixed absolute
tolerance T, the active rule is:

```text
pass         if hi <= T
regression   if lo > T
inconclusive otherwise
```

Thus an interval ending exactly at T passes; one starting at T and extending
above it is inconclusive. Higher-is-better intervals are reversed and negated
before comparison. With seven synthetic baseline values `[2000]*7` and current
`[2050]*6 + [2300]`, the 95% adverse interval is `[50, 300]` ms: the confidence
gate is **inconclusive, exit 3**, whereas the MAD gate passes (median change
50 ms, MAD zero). Current `[2050] + [2200]*6` gives `[50, 200]` ms and is also
inconclusive, whereas the MAD gate regresses. These cases are reproducible in
`tests/lighthouse/test_confidence_gate.py`; they are synthetic contracts, not measurements.

This mode uses the confidence interval for classification. `noise_multiplier`
still appears in the retained MAD evidence but does not widen the confidence
interval. JSON marks `confidence.affects_gate: true`, preserves each metric's
`mad_decision` and `mad_reasons`, and reports the active decision/reasons in the
usual fields; Markdown identifies the active gate. Default reports stay
unchanged. Saved baselines support either mode with the same pinned policy.

Missing or insufficient observations yield inconclusive without falling back
to MAD. Invalid inputs/statistics stay invalid; an invalid confidence interval
(including endpoint overflow) makes the active confidence gate invalid, exit 2.
Known invalid groups leave valid groups usable; overall precedence remains
invalid, regression, inconclusive, pass. The option requires an explicit valid
confidence level; missing levels and any nonzero relative tolerance are
configuration errors (library `ValueError`; CLI exit 2 before output writes).

The same iid and per-group/metric coverage assumptions above apply. There is
no simultaneous guarantee across many gate decisions, and no prediction or
real-site false-positive claim. The default remains the raw MAD gate.

## Gate exit codes

| Exit | Meaning |
| --- | --- |
| 0 | All group/metric decisions pass |
| 1 | At least one valid comparison regresses, with no invalid input |
| 2 | Invalid input/configuration, mismatch, usage or report I/O failure |
| 3 | At least one comparison is inconclusive, with no regression or invalid input |

Aggregate precedence is invalid, regression, inconclusive, pass. Every group
and metric keeps its own decision and reasons. Data-invalid reports are still
written; config/argument failures print an invalid JSON diagnostic to stderr
and write no report. CLI usage errors use argparse's usage diagnostic.
Markdown includes a readable summary and complete JSON evidence, with every
selected run value, missing/invalid reason, run warning and group identity.

## Explicitly supported input

Only **Lighthouse 12.8.2 navigation LHR JSON** is supported and checked. Both
`finalUrl` and `finalDisplayedUrl` must be absolute HTTP(S) URLs. Upstream marks
`finalUrl` deprecated; this initial dialect deliberately still requires it.
No URL normalization, redirect inference, version fallback or mismatch override
is performed. A different/future Lighthouse version fails explicitly.

Group identity includes the exact URLs, Lighthouse version, navigation mode,
**entire `configSettings` object**, `userAgent`, and the host/network user agents
in `environment`. Object key order is ignored; list order and all values are
preserved. Even changes to output/locale/unknown settings create distinct groups.
This conservative policy can reject harmless differences; regenerate comparable
reports rather than silently ignoring fields.

Required config dimensions are explicit `formFactor` (mobile/desktop),
`throttlingMethod` (simulate/devtools/provided), all six numeric throttling
settings, and complete screen emulation (width, height, deviceScaleFactor,
mobile, disabled). No omitted setting is filled with a guessed default.
Only selected numeric audit fields are projected; unrelated audits, category
scores, screenshots and renderer metadata are intentionally unused. A selected
audit's `id` must match its key and its unit must match the policy.

`runtimeError`, selected audit errors, malformed JSON/UTF-8, duplicate keys,
non-finite numbers, invalid numeric types/units, unsupported modes/versions,
empty input directories and unmatched groups all invalidate the gate. Malformed
files retain their filename and parse error in the report; their unparseable
contents cannot be treated as measurements. Benchmark index and run warnings
are retained but not grouped; equal metadata cannot prove equal hardware,
collection conditions, custom audit implementation or site state.

A development smoke check also loaded the hash-verified full upstream sample
and compared identical copies successfully. That checks interoperability, not
independent performance measurements; all committed tests stay synthetic/offline.

This is a selected-fields reader, not a validator of the entire upstream LHR schema.
LHCI manifests, wrapped reports, timespan and snapshot reports are unsupported.
The baseline argument additionally accepts this tool's saved baseline manifest.

## Reproduce the synthetic experiment

```sh
uv run --extra test pytest tests/lighthouse -q
python examples/lighthouse/experiment.py
python examples/lighthouse/experiment.py --out /tmp/lighthouse-experiment.json
```

Seed **37037**, 180 labeled scenarios: 60 unchanged/noisy, 60 large shifts,
60 small/noisy shifts. Seven LHRs per side; two extra runs per side in a separate
low-sample case. The baseline comparator uses the **first run on each side**
with the same tolerance, without MAD. The spread-aware policy uses all runs.
This compares policies, not collection speed; seven-run collection costs more.
Family distributions and outlier injection are documented in `examples/lighthouse/README.md`.
All scenario values, statistics, decisions and reasons are committed in
`examples/lighthouse/experiment-results.json`; tests regenerate and check them.

Measured on CPython 3.14.4, with synthetic data only:

| Outcome | Single-run gate | Spread-aware gate |
| --- | ---: | ---: |
| False positives / 60 unchanged scenarios | 24 | 0 |
| False negatives (shifted decided pass) / 120 shifts | 32 | 0 |
| Shifted inconclusive / 120 shifts | 0 | 57 |
| **Total undetected shifts (pass + inconclusive)** | **32** | **57** |
| Unchanged inconclusive / 60 unchanged scenarios | 0 | 56 |
| All inconclusive / 180 scenarios | 0 | 113 |
| Separate two-run case | No minimum-sample policy | Inconclusive |

The spread-aware gate abstains often and detects fewer labeled shifts overall.
A lower false-positive count on these generated data is **not evidence of
real-site improvement**, statistical significance, proven confidence intervals,
or a guaranteed false-positive rate. Both error definitions and abstentions
must be read together. No real pages were tested. Adoption, demand, stars and
real-world gains remain **unverified**.

## Scope

No Chrome runs, hosted CI, deployment, causal attribution, or production-data
collection is included. Runtime accepts local files and writes only explicitly
requested reports. Tests run serially.

## License

Original implementation: [MIT](../LICENSE). Upstream format projects are Apache-2.0;
see [THIRD_PARTY.md](../THIRD_PARTY.md).
