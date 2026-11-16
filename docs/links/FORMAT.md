# qawrench links: supported JSON and accounting contract

This adapter accepts a strict subset of the report serialized by
`lychee-v0.18.1`. It also tests the report in issue #1637 (2025-02-18; that
report does not declare its Lychee version). No claim is made that the issue
was produced by v0.18.1. There is no format version field, so structural checks
cannot prove the producing version; users must select the documented profile.

## Input schema

All of these top-level keys are required, with no additional keys:

```text
total successful unknown unsupported timeouts redirects excludes errors cached
success_map error_map suggestion_map excluded_map duration_secs detailed_stats
```

Counters and `duration_secs` are nonnegative JSON integers, excluding booleans.
`detailed_stats` is a boolean. Each map is an object. In the three response maps,
keys are readable canonical relative source paths and values are arrays of
objects with exactly `url` (nonempty single-line string) and `status`. Additional
response keys are rejected, including newer Lychee position fields. URLs need
not be HTTP URLs and no URL is fetched. Literal manifests require exact source
text; opt-in Markdown mapping compares decoded Markdown destinations as described
below. JSON must be UTF-8, with no duplicate object keys or NaN/Infinity.

`status` has required string `text`, optional integer `code` (100..999), or
optional string `details`. Code and details are mutually exclusive, following
the upstream serializer's branch order. Unknown fields/text are rejected.

| Context | Accepted status | Interpretation |
| --- | --- | --- |
| `error_map` | `{"text":"Network error","code":404}` | HTTP failure (any valid code value retained) |
| `error_map` | `{"text":"Network error","details":"..."}` or `{"text":"Network error"}` | Transport error; original details retained |
| `error_map` | `{"text":"Timeout"}` or `{"text":"Timeout (408)","code":408}` | Timeout; code, if present, must match text |
| `success_map` | `{"text":"200 OK","code":200}` | Success ignored for findings; numeric text prefix must match code |
| `excluded_map` | `{"text":""}` | Explicitly excluded, not a failure |

HTTP acceptance is determined by map membership. A success may have a non-2xx
code because Lychee permits accepted-status overrides. Successful text needs
a matching decimal code followed by a space and a nonempty, single-line reason.
The normalization library recognizes upstream's `Unsupported: ...` status, but
the v0.18.1 report does not store such records in `error_map`, so placing one there
is rejected. The positive `unsupported` counter is supported as an aggregate.
Other real Lychee error variants, including missing fragments/files, GitHub-client
errors, and cached status texts are outside this tested subset and fail explicitly.

## Counters and loss prevention

The adapter requires:

```text
total = successful + unknown + unsupported + timeouts + redirects + excludes + errors
cached = unknown = redirects = 0
suggestion_map = {}
count(non-timeout error_map records) = errors
count(timeout error_map records) = timeouts
```

Cached is an overlapping counter, not another term in the total. It is rejected
here because upstream caching changes both status representation and map storage.
Unknown/redirect records aren't retained in the tested upstream maps; those
nonzero counters are rejected rather than disappearing. Nonempty suggestions
are rejected rather than being silently discarded.

When `detailed_stats` is false, success/excluded maps must contain zero records.
Their counters still contribute to the summary. When true, their map lengths must
equal their respective counters. Duplicate URLs within a source/map are rejected.
Upstream's maps use sets, so a report whose counters exceed the distinct retained
records may be genuine but is outside this profile: it fails with a count error.

A positive unsupported counter produces one unlocated aggregate warning with
`summaryOnly: true` and `upstreamCount`. This explicitly retains information that
the source JSON cannot attribute to individual URLs. No source or line is invented.

## Manifest and SARIF

Manifest: an array of `{source,url,line,column}` objects with an optional
`occurrenceId` string; no other keys.
Source/URL pairs must occur in `error_map`. Positions are positive JSON integers,
not booleans. Duplicate source/URL/position entries fail. Files are UTF-8, decoded
without normalization of Unicode; CRLF and CR are normalized to LF for line
indexing. Only LF delimits lines after that normalization. Tabs and combining
characters each count as individual code points. End columns are exclusive.

For each occurrence the literal slice
`line_text[column-1 : column-1+len(original_url)]` must equal the entire original
URL. This verifies a literal span, not token boundaries or Markdown semantics.
Every supplied entry must validate; no best-effort skipping. A partial manifest
maps only its asserted occurrences, without guessing extra occurrences. A pair
with no manifest entries gets exactly one file-level result, without `region`.

Alternatively, `--markdown` / `convert(..., markdown=True)` selects the bounded
Markdown reader documented in [docs/links.md](../links.md#markdown-occurrence-mapping).
It is mutually exclusive with any supplied manifest. Only supported syntactic
link destinations are matched to the entire original URL after Markdown escape
and entity decoding; report URLs and source keys are never normalized. Inline
regions span original destination text (so their length can differ from the
report URL); reference regions span each complete bracketed use, not definitions.
Missing matches remain file-level. Unsupported uses are not an exhaustiveness
error. All prior source confinement, UTF-8, column, sorting, accounting and
output protection rules apply in both modes. Run property `occurrenceMapping`
records `literal-manifest` or `markdown-bounded-v1`.

Each result contains a rule ID/index, message, category, original source and full
original entry, `mapped`, and a SHA-256 partial fingerprint. Four rules cover HTTP,
timeout, transport and unsupported. Rule order is fixed; results are sorted by
source, original URL, line and column; the optional aggregate comes last. The
fingerprint hashes compact UTF-8 JSON of category/source/URL/region, using sorted
keys; status detail and absolute root are excluded. Positions moving change the
position fingerprint. Original status fields are never modified. `%SRCROOT%` explicitly
records the resolved source root; relative artifact URIs are percent-encoded.

An optional manifest `occurrenceId` is a nonempty UTF-8 string without C0/DEL
control characters. The tuple `(source, original URL, occurrenceId)` must be
unique. Its compact UTF-8 JSON array SHA-256 is stored separately as
`lycheeStableOccurrence/v1`; category, status and region are excluded. The caller
maintains IDs across moves; new occurrences need new IDs. Literal span validation
is always required, including when an ID is supplied.

Baseline input is a previous single-run adapter SARIF 2.1.0 report. Tool/rules,
column kind, input dialect, occurrence mapping mode and `originalUriBaseIds`
must match the current run exactly. Canonical result reconstruction validates
source paths, original entries, regions, artifact mappings and fingerprints;
duplicate identities/positions are rejected. Previous comparison annotations
are ignored for matching. Historical files are never read. Other tools' SARIF,
automatic source-root relocation, and cross-file rename inference are unsupported.

Identity is source/URL/ID when an ID is present; otherwise it is source/URL/exact
region (null for a file-only finding). These namespaces do not fall back to each
other. The aggregate has one distinct identity. Category/diagnostic changes at
the same identity are updates. Current results gain `baselineState: new`,
`unchanged`, or `updated`; changed regions for matching IDs also gain
`properties.baselineMatch: moved` and `baselinePreviousRegion`. Other matches
use `baselineMatch` equal to the SARIF state. Only current results are emitted.
`summary.baselineComparison` counts `new`, `unchanged`, `updated`, `moved` (a
subset of updated), and `absent`. Absent is evidence missing from the current
report, not proof of a fix, exclusion, or source deletion. All existing summary
counts and CI failure behavior continue to refer to current results.

Summary fields:

| Field | Meaning |
| --- | --- |
| `inputErrors`, `inputTimeouts` | Original respective Lychee counters |
| `inputFindings` | Total error_map records = errors + timeouts |
| `mappedFindings`, `unmappedFindings` | Input records with/without valid occurrences in the selected mode; sum equals inputFindings |
| `mappedResults` | Total verified occurrences emitted |
| `ignoredSuccess`, `excluded` | Original successful/excluded counters |
| `unsupportedChecks` | Original unsupported counter; individual data unavailable |
| `aggregateResults` | 1 if unsupportedChecks is positive, otherwise 0 |
| `resultCount` | mappedResults + unmappedFindings + aggregateResults |

Run properties additionally preserve all original counters, duration and
detailed-stats mode. Paths and source contents are read from a static trusted
snapshot; confinement checks do not defend against concurrent directory swaps.
The converter reads complete reports and source files into memory; it is not a
streaming processor or a sandbox for arbitrarily large hostile inputs.

Primary sources: [response](https://raw.githubusercontent.com/lycheeverse/lychee/lychee-v0.18.1/lychee-lib/src/types/response.rs),
[status](https://raw.githubusercontent.com/lycheeverse/lychee/lychee-v0.18.1/lychee-lib/src/types/status.rs),
[stats](https://raw.githubusercontent.com/lycheeverse/lychee/lychee-v0.18.1/lychee-bin/src/stats.rs),
[JSON formatter](https://raw.githubusercontent.com/lycheeverse/lychee/lychee-v0.18.1/lychee-bin/src/formatters/stats/json.rs).
