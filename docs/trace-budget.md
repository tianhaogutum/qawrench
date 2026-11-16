# qawrench trace-budget

`qawrench trace-budget` (formerly `trace-size-budget`) is an offline CLI and library
for Playwright trace ZIP inventories, before/after CI byte budgets, and verified
lossless ZIP recompression.

It inventories every member, validates CRCs and trace envelopes, then optionally
repackages **all filenames and every decompressed byte** using ZIP_DEFLATED.
It never removes events, screenshots, resource bodies, attachments, or unknown
members. Recompression can save nothing or increase size; the CLI reports both.

Playwright already provides trace recording, viewing, and capture controls.
This tool adds allocation reports and verified before/after comparisons for
existing archives, including an explicit audit of whole members omitted by
another tool. It does not reconstruct actions, shrink images, collect garbage,
replace the viewer, or promise real-site improvements.

## Install

Requires Python 3.11 or newer. Runtime and tests use only Python's standard
library, with no network requests, servers, browser installs, or third-party
runtime dependencies.

```sh
pipx install qawrench               # or: pip install qawrench
qawrench trace-budget --version     # prints the bare qawrench version, e.g. 0.1.0

# Or run without installing:
uvx qawrench trace-budget --help
```

`python -m qawrench trace-budget` is equivalent to `qawrench trace-budget`.
Python and zlib versions influence compressed output; the benchmark measurements
below identify both.

The example scripts live in `examples/trace_budget/` in the qawrench repository;
run the commands below from the repository root.

## Working examples

The generator writes a **synthetic trace-shaped ZIP**, never a browser recording
or production capture. It uses version-8 event envelopes and a synthetic HAR-like
resource snapshot. Its exclusive-create output requires a fresh path.

```sh
python3 examples/trace_budget/generate_trace.py /tmp/trace.zip
qawrench trace-budget inspect /tmp/trace.zip --out /tmp/trace-report.json
qawrench trace-budget repack /tmp/trace.zip --out /tmp/trace-repacked.zip
python3 examples/trace_budget/benchmark.py
```

All subcommands print JSON to stdout. `inspect --out` and `compare --out`
additionally write the same JSON atomically. Use `--force` explicitly to replace
an existing report or archive. Input/output aliases (including hardlinks and
symlinks) are refused.
Output parents must already exist; no input member is extracted to disk.

For a CI archive-size budget and five largest contributors:

```sh
qawrench trace-budget inspect /tmp/trace.zip --budget-bytes 500000 --top 5
qawrench trace-budget repack /tmp/trace.zip --out /tmp/trace-planned.zip --dry-run
```

The first example exits **2** because the synthetic archive exceeds 500,000
bytes. Exit **0** means success/within budget; **1** means invalid archive,
unsupported dialect, limit, or I/O failure. Argument usage errors also exit 2.
An exceeded budget still emits the complete valid report. Equality to the budget
passes. `--budget-basis archive` (default) measures the actual ZIP file including
headers; `stored` sums compressed payload bytes; `uncompressed` sums member
bytes. Byte arguments are decimal integer counts, not strings such as `10MB`.
Budgets apply to `inspect` and `compare`; `compare` checks the after archive
and reports the before measurement against the same limit. Repack itself does
not enforce a byte budget.

`repack --dry-run` validates all input members and output-path rules, then reports
the plan. It creates no file, executes no script, and does not estimate savings.
Ordinary repack also never executes anything in an archive.

Library use:

```python
from qawrench.trace_budget import Limits, compare_archives, inspect_archive, repack_archive

inventory = inspect_archive("/tmp/trace.zip", budget_bytes=500_000, top=5)
assert inventory["allocation"]["resources"]["member_count"] == 1
assert inventory["resource_allocation"]["network_bodies"]["member_count"] == 1
result = repack_archive("/tmp/trace.zip", "/tmp/trace-library-repacked.zip")
assert result["all_member_hashes_equal"]
print(result["original_bytes"], result["new_bytes"], result["ratio"])
audit = compare_archives("/tmp/trace.zip", "/tmp/trace-library-repacked.zip",
                         budget_bytes=500_000)
assert audit["all_retained_hashes_equal"]
assert audit["loss_ledger"]["members"] == []
```

The API raises `ArchiveError` for archive/policy errors and `OSError` for I/O
errors; the CLI converts both to a concise stderr diagnostic and exit 1.

## Before/after comparison and explicit loss audit

Every successful repack now includes a `comparison` object. Audit two existing
archives independently with the same report shape:

```sh
qawrench trace-budget compare /tmp/trace.zip /tmp/trace-repacked.zip \
  --budget-bytes 500000 --out /tmp/trace-comparison.json
```

`totals` and `allocation` contain `before`, `after`, and signed `delta` objects
(**after minus before**, so a reduction is negative). Allocation uses the same
`trace`, `network`, `resources`, and `other` categories as inspection. Totals also
include ZIP overhead; the stored-byte category deltas plus overhead delta equal
the archive-byte delta. `event_allocation` groups trace/network events by their
exact `type`, with counts and uncompressed line bytes. Bytes include each line's
original newline, CR in CRLF, whitespace, and UTF-8 bytes; a final unterminated
line has no added newline. Counts and bytes aggregate across all streams. Empty
network streams contribute no events. Compressed bytes cannot be attributed
exactly to individual event types and are reported only at member/category level.

The comparison requires exact names, uncompressed sizes, SHA-256s, CRC32s, and
directory identities for all retained members. `retained_payloads` lists every
retained name, size, and both independently computed SHA-256s, including unknown
members and empty directories. `all_retained_hashes_equal` is true only after
these checks pass. A changed retained payload, even only its final newline, or
an added member exits **1**, with no success JSON or report replacement.

Omitted members also fail by default. If an external process intentionally
omitted whole members, opt into auditing those losses explicitly:

```sh
qawrench trace-budget compare before.zip externally-reduced.zip \
  --allow-removals --out loss-audit.json
```

`loss_ledger` records `removals_allowed`, removed member/byte totals, and a sorted
`members` list with the original name, category, size, SHA-256, CRC32, directory
identity, and event counts/bytes for removed streams. A lossless comparison has
an explicit empty ledger. For example, if `resources/optional.bin` containing
`b"optional"` is absent from the after ZIP, the default comparison fails;
`--allow-removals` reports one removed resource, 8 uncompressed bytes, and its
original SHA-256, while requiring every retained payload to match. This flag
performs no deletions and cannot authorize changed retained streams or additions.

Both archives must independently pass the normal format, CRC, and policy checks,
including at least one valid version-8 trace stream. This is an audit of exact
payload retention, not proof that references in an externally reduced trace
still resolve or that its viewer behavior is preserved. Known-slot diagnostics
below can identify some unresolved references, without a full integrity audit. The tool does not prune
resources or remove individual events within a retained stream.

`budget.measured_bytes` and `budget.exceeded` describe the after archive;
`before_measured_bytes` and `before_exceeded` apply the same basis and limit to
the before archive. Equality passes. An exceeded after budget exits **2** and
still writes/prints the complete valid comparison. Validation or parity failure
exits **1** and preserves any existing report even with `--force`. Report output
cannot alias either input, including hardlinks and symlinks. Both inputs must
remain unchanged throughout the operation; each scan checks its open file's
metadata, but the two scans are sequential and are not a shared snapshot.

## Report and preservation contract

`allocation` groups members into `trace`, `network`, `resources`, and `other`.
Non-directory `resources/*` wins over suffix classification; other `.trace` and
`.network` names select NDJSON validators. Directories belong to `other`.
Each group includes member count, `stored_bytes` (compressed payload, even for
ZIP_STORED), and `uncompressed_bytes`. `members` includes every name, SHA-256,
CRC32, method, sizes, and stream event counts and exact uncompressed bytes by
type. `top_members` sorts compressed sizes descending; `top_uncompressed_members`
provides the other view. Name order breaks ties. ZIP header/comment/extra overhead
is reported separately.

### Resource reference roles

`inspect` reports `resource_allocation`, partitioning non-directory `resources/*`
members into five exclusive groups. Each reports `member_count`, `stored_bytes`,
and `uncompressed_bytes`; their sums equal `allocation.resources` for every
metric. Every resource member also has a sorted, distinct `resource_roles` list
and a `resource_group`:

| Group | Observed version-8 references |
| --- | --- |
| `screencast_screenshots` | Only `screencast-frame.sha1` |
| `network_bodies` | Only `resource-snapshot.snapshot.request.postData._sha1` or `.response.content._sha1` |
| `other_referenced` | Only other nested `sha1`, `_sha1`, or keys ending in `Sha1` |
| `shared` | Two or more of those roles; the member's list identifies each |
| `unreferenced` | No matching supported-key reference; the role list is empty |

Specific roles follow the pinned [trace event types](https://github.com/microsoft/playwright/blob/v1.56.1/packages/trace/src/trace.ts)
and [HAR body fields](https://github.com/microsoft/playwright/blob/v1.56.1/packages/trace/src/har.ts).
Other keys follow the recorder's recursive `visitTraceEvent` convention,
conservatively including attachments, DOM resource overrides and opaque extension
fields. References aggregate across every validated `.trace` and `.network`
stream, including resource-snapshot events in trace streams. Repeated references
in the same role count the member once. A resource referenced as both a screenshot
and a network response belongs only to `shared`, with roles
`["network_bodies", "screencast_screenshots"]`. Its bytes are never counted twice.

Matching uses the exact member name `resources/<reference value>`, with no path
normalization, basename matching, MIME/extension guessing, or content deduplication.
The identifier need not be a hexadecimal SHA-1: the pinned recorder also uses
page/timestamp JPEG names. Identical payloads under different names remain
separate members. Resource directories and non-resource files are excluded.
Empty/non-string values, malformed nested shapes and unresolved references do
not create resource membership or byte totals and do not fail valid envelopes.
They remain byte-preserved. Roles for existing resources are bounded by member
count; known-slot missing-reference samples have the fixed bounds below.
Event traversal stays within the existing NDJSON line limit.

`unreferenced` means no reference observed under these key rules, **not proof of
unused or safe-to-delete content**. `other_referenced` is conservative attribution,
not validation of extension semantics. Image validity, complete reference integrity and
viewer behavior are not verified. No pruning is performed.

`compare.resource_allocation` (also in `repack.comparison`) gives each group's
`before`, `after`, and signed `delta`. Retained resource entries additionally
include `before_resource_roles` and `after_resource_roles`; removed resources in
the loss ledger retain their original roles/group. Removing an entire stream
with `--allow-removals` may reclassify a retained resource, for example from
`shared` to `screencast_screenshots` after removing its network stream. Exact
retained names, bytes, sizes, SHA-256s, CRC32s and directory identities must still
match. Changing just a reference in a retained stream fails parity.

The synthetic multi-role contract and its negative cases are reproducible with:

```sh
uv run --extra test pytest tests/trace_budget/test_resource_roles.py -q
```

### Bounded missing-reference diagnostics

`inspect.resource_reference_diagnostics` reports unresolved references separately
from existing-member allocation. It observes only the three exact screencast/HAR
slots in the table above, across all validated streams. A nonempty string counts
as missing unless `resources/<value>` exactly names an existing non-directory
resource. Case, whitespace, path text and percent escapes are never normalized.
The value is never opened as a filesystem path. Absent/empty/non-string values
and malformed object ancestors are ignored; opaque extension references do not
produce missing diagnostics. Otherwise valid envelopes continue to pass.

- `missing_occurrences` counts every unresolved slot, including repeated values
  and both request and response fields in one event.
- `missing_occurrences_by_role` always includes `screencast_screenshots` and
  `network_bodies`, even at zero. These are occurrence counts, not distinct
  identifiers or resource-member counts.
- `sample_limit` is **20** and `sample_text_limit` is **160** Unicode characters
  per text field. `samples` contains the first 20 missing occurrences, with
  `role`, dotted `slot`, 1-based `line`, `stream_prefix`, `reference_prefix`, and
  explicit `stream_truncated` / `reference_truncated` booleans. Repeated values
  occupy separate sample slots. Order is decoded member-name order, then line
  order, then request before response. Physical ZIP member order is irrelevant.
- `sample_omitted_occurrences` is total missing occurrences minus sample length;
  counting continues after the cap. Persistent diagnostics keep two counters
  and at most 20 clipped records, with no growing set of unresolved identifiers.
  Counter magnitudes are bounded by the configured total uncompressed input
  limit; temporary event parsing remains subject to the line limit.

`compare.resource_reference_diagnostics` (also in `repack.comparison`) contains
full independent `before` and `after` diagnostics. Its `delta` gives signed
`missing_occurrences` and `missing_occurrences_by_role` counts, after minus before;
there is no sample-list delta. Removing a resource externally may increase
missing counts; removing a stream may decrease them. `--allow-removals` remains
mandatory for whole-member omissions. Missing references add **zero members and
zero bytes**, authorize no pruning and never relax retained-payload parity.
Dry-run continues to return its validation/plan-only report.

For a minimal synthetic example, create a fresh ZIP using only the standard
library, then inspect it with the installed CLI:

```sh
python3 - <<'PYEXAMPLE'
import json, zipfile
context = dict(type="context-options", version=8, browserName="chromium",
               platform="linux", origin="library", options={},
               wallTime=0, monotonicTime=0)
frame = dict(type="screencast-frame", sha1="missing.jpeg")
network = dict(type="resource-snapshot", snapshot={
    "response": {"content": {"_sha1": "present-body"}}})
with zipfile.ZipFile("/tmp/trace-missing.zip", "x") as z:
    z.writestr("trace.trace", "\n".join(map(json.dumps, [context, frame, frame])) + "\n")
    z.writestr("trace.network", json.dumps(network) + "\n")
    z.writestr("resources/present-body", b"body")
PYEXAMPLE
qawrench trace-budget inspect /tmp/trace-missing.zip --top 0
```

Expected: exit **0**, two missing screenshot occurrences, zero missing network
body occurrences, two sample entries, and one 4-byte network-body member. A
lossless repack preserves those diagnostics and every payload. Focused contracts:

```sh
uv run --extra test pytest tests/trace_budget/test_missing_references.py -q
```

### Verified repack

Repack first validates/hashes the source, streams it into a temporary ZIP in the
output directory, closes it, reopens it, validates CRCs, then compares the exact
set of member names, sizes, SHA-256s, CRC32s, and directory identities. A failed
check leaves an existing destination intact and removes the temporary file.
Source file metadata is checked for concurrent changes. Inputs must remain
unchanged during a run; this is not a filesystem snapshot or locking service.

Payload verification reads each member's full declared compressed extent and
computes CRC32 from the decoded bytes. DEFLATE must reach its end marker exactly
at that extent: missing end markers, trailing compressed data (including a second
stream), and decoded sizes that differ from the central directory are rejected.
A matching CRC for a decoded prefix does not make an incomplete stream valid.
Diagnostics identify the member; CRC mismatches include expected and actual
checksums. These checks run before repacking, again while copying, and on the
candidate archive. A corrupt source cannot publish a candidate, even with
`--force`; both the source and any existing destination remain unchanged.

For example, the regression fixture truncates the final compressed byte of a
4,096-byte `resources/body`, while leaving all decoded bytes and their matching
CRC available. `inspect` and `repack` exit **1** with
`truncated DEFLATE stream: 'resources/body'; end marker missing`, emit no success
JSON, and leave an existing output intact. Run that local synthetic example with
`uv run --extra test pytest tests/trace_budget -k truncated_deflate -q`.

Publication is atomic: a same-directory hardlink provides no-clobber publication,
or `os.replace` replaces an explicitly authorized existing file. Output symlinks
and special output files are refused. Use a local filesystem with atomic rename
and hardlink support. Temporary archive contents are fsynced; power-loss directory
durability is not guaranteed.

Member order is sorted by decoded filename, timestamps are 1980-01-01 00:00:00,
permissions are Unix 0644 (files) / 0755 (directories), and ZIP comments and extra
fields are cleared. All members, including empty directories and already
compressed resources, use DEFLATE at `--level 9` (configurable 0–9). ZIP64 is
read through Python's zipfile for the central directory, with the local checks
below, and written when needed and allowed by configured limits.
**Member contents and names are lossless; ZIP metadata and raw container bytes
are not preserved.** Repeated repacks are deterministic with the same Python,
zlib, level, and member set; no cross-version compressed-byte guarantee is made.
Even a larger output is published when verification succeeds; choose whether
to keep it using the reported signed savings. Repack never silently skips data.

## Supported input and limits

Compatibility reference: [Playwright v1.56.1 trace types](https://github.com/microsoft/playwright/blob/v1.56.1/packages/trace/src/trace.ts)
and [recorder](https://github.com/microsoft/playwright/blob/v1.56.1/packages/playwright-core/src/server/trace/recorder/tracing.ts).
The implementation is independently written.

- At least one non-resource `.trace` stream is required. **Every** such stream
  must start with `context-options` and integer `version: 8`; its required context
  envelope fields are checked. Older and future versions fail explicitly.
- All trace lines must be UTF-8 JSON objects with a nonempty string `type`.
  Subsequent version-8 event payloads and unknown type/field extensions are
  retained opaquely, without semantic validation or action reconstruction.
- `.network` streams may be empty; otherwise each line must have
  `type: resource-snapshot` and an object `snapshot`. Nested HAR/snapshot fields,
  resource references, and screenshot validity are not semantically validated;
  supported reference keys are observed for role allocation and bounded known-slot
  missing-reference diagnostics.
- CRLF and a final line without newline are supported. Empty lines, BOM, malformed
  JSON, duplicate JSON keys, and non-JSON numeric constants fail. Unknown archive
  members remain byte-preserved in `other` or `resources`.
- Supported ZIP member compression is STORED or DEFLATED. Encryption, duplicate
  names, file/parent collisions, symlinks/special members, nonempty directories,
  unsafe paths (`..`, absolute paths, backslashes, colons, control characters),
  CRC failures, and malformed ZIPs fail. No extraction occurs.

### Local headers, data descriptors, and ZIP64

Every member must have the same two-byte **version needed to extract**,
compression method, and all 16 flag bits in its local header and central directory.
Extraction declarations must have a zero high byte and a numeric version from
10 through 45 (ZIP 1.0–4.5), with these feature minima:

| Member features | Minimum extraction version |
| --- | ---: |
| STORED file | 10 (1.0) |
| DEFLATED or directory | 20 (2.0) |
| Local or central member ZIP64 extra, even for small/empty payloads | 45 (4.5) |

The highest applicable minimum wins. A matching declaration above its feature
minimum is accepted up to 45; higher requirements are unsupported by this tool.
A descriptor alone adds no extra minimum, so STORED 1.0 descriptors remain
accepted. The independent `version made by` field is not an extraction requirement.
Local/central mismatch diagnostics include both numeric values; low/unsupported
requirements report the minimum/maximum. For example, local 99 versus central 20
on `resources/body` raises
`local ZIP extraction version mismatch: 'resources/body'; local 99, central 20`.

A ZIP64 EOCD has a separate two-byte **version needed to extract** at offset
+14. This tool requires **exactly 45 (4.5), with a zero high byte**: lower values
underdeclare ZIP64, and higher requirements are unsupported. This applies even
when all members use ordinary sizes and member extraction version 20. The end
record's `version made by` at +12 is independent and is not restricted by this
policy. For example, an otherwise valid end declaring version 99 raises
`unsupported ZIP64 EOCD extraction-version declaration: 'trace.zip'; required 99, supported 45 (4.5)`.
The archive-naming check runs before ZIP directory parsing and payload decoding
for inspection, both comparison inputs, repack, dry-run, and completed repack
candidates. The CLI exits **1** with empty stdout; inputs and existing forced
reports/archives are preserved, and invalid candidates are removed. Supported
version-45 ends retain exact reports and all decompressed member bytes.

Run the archive-level extraction-version contracts with:

```sh
uv run --extra test pytest tests/trace_budget/test_zip_end_versions.py -q
```

Matching flags must also fit the method-specific supported mask:

| Compression method | Supported mask | Permitted set bits |
| --- | --- | --- |
| STORED (0) | `0x0808` | 3 (data descriptor), 11 (UTF-8) |
| DEFLATED (8) | `0x080e` | 1–2 (all four DEFLATE option combinations), 3, 11 |

All other bits fail, including encryption, masked local metadata (bit 13),
patched data, and unused/reserved requirements. Bits 1–2 fail on STORED.
For example, matching bit 13 on an otherwise readable DEFLATED `resources/body`
raises `unsupported ZIP flags: 'resources/body'; unsupported mask 0x2000, supported mask 0x080e`.
The unsupported mask excludes permitted bits, even in a mixed declaration.
UTF-8 and legacy CP437 filenames remain supported. Flags are normalized during
repack; every decoded filename and payload byte is preserved.

Every member's central **disk number start** must be literal zero. Nonzero
values require unsupported disk placement, even when all readable bytes happen
to be in one file. A `resources/body` member declaring disk 1 raises
`unsupported ZIP disk start: 'resources/body'; declared 1, supported 0`.
The ZIP64 disk-start sentinel `0xffff` is also unsupported, including when its
extra field contains disk zero: Python's ZIP reader leaves this declaration
unresolved. This conservative policy does not restrict ordinary ZIP64 sizes or
offsets with a literal disk-zero field. It applies to every member category and
to the completed repack candidate before publication.

Archive end records must also declare a single disk. Both the classic EOCD
**current disk** and **central-directory start disk** must be literal zero,
even when ZIP64 end records are present. A ZIP64 EOCD requires both disk fields
to be zero; its locator requires end-record disk zero and total disks **one**.
Nonzero disk sentinels are unsupported. Raw declarations are checked before
Python's ZIP reader can overwrite classic fields with ZIP64 values, and before
any payload decoding. For example, current disk 1 on `trace.zip` raises
`unsupported ZIP archive disk declaration: 'trace.zip'; classic EOCD current disk 1, supported 0`.
Errors identify the archive, record/field, declared value, and supported value.
These rules cover inspection, both comparison inputs, dry-run, and the completed
repack candidate. Failure emits no success JSON, preserves existing forced
outputs, and removes a failed candidate. ZIP64 sizes/offsets with supported disk
declarations keep the ordinary report shape and exact decoded payloads.

End-record **entries on this disk** and **total entries** must agree. Without
ZIP64, both classic counts must be equal and neither may be `0xffff`. This tool
treats that classic value as a ZIP64 sentinel requiring a readable ZIP64 EOCD.
With ZIP64, its two 64-bit counts must be equal; each classic count must
independently be `0xffff` or match its corresponding ZIP64 value. Mixed classic
literal/sentinel counts are supported. The 64-bit value `65535` is a literal
count, not a sentinel. Contradictory classic literals cannot be masked by ZIP64.
For example, a classic end declaring 0 entries on disk and 3 total raises
`inconsistent ZIP end-record entry counts: 'trace.zip'; classic EOCD entries on this disk 0, total entries 3`.
These archive-naming checks run before decoding for every operation above,
including completed candidates. The CLI exits **1** with empty stdout and
preserves inputs and existing forced outputs. Valid equal-count layouts keep
the report shape and every decompressed payload.

The resolved end total must also equal the actual number of parsed central-directory
entries: the ZIP64 total when present, otherwise the classic total. Zero is a
literal count, not an unspecified value. Every entry counts, including empty files
and directories; this is not a count of unique filenames or trace streams.
For example, matching end counts 2/2 with three directory entries raise
`ZIP central-directory entry count mismatch: 'trace.zip'; declared 2, actual 3`.
This check follows end-record agreement checks and ZIP directory parsing, before
member validation or payload decoding, for every operation and completed candidate.
Malformed/unreadable directories may fail earlier in Python's ZIP reader. A valid
empty ZIP with count zero still fails the separate requirement for a trace stream.
Correct totals retain the exact report shape and decompressed member bytes.

Classic and ZIP64 **central-directory size** and **offset** declarations must
also agree. Each classic field independently uses `0xffffffff` as a sentinel
requiring a readable ZIP64 EOCD, or a literal value that must equal its ZIP64
counterpart. Zero is a literal; the 64-bit value `4294967295` is also literal.
Both mixed literal/sentinel combinations are supported. Offsets are compared
as **raw declarations**, before Python adjusts for prepended data; adding a
prefix length to only the classic offset cannot reconcile a contradiction.
Matching offsets may both include or both exclude a prefix.

For example, classic directory size 0 and ZIP64 size 163 raise
`inconsistent ZIP central-directory declarations: 'trace.zip'; classic EOCD central-directory size 0, ZIP64 EOCD central-directory size 163`.
Checks run before directory parsing or payload decoding for inspection, both
comparison inputs, repack, dry-run, and completed candidates. Errors identify
the archive, field and both values; the CLI exits **1** with empty stdout,
preserves both inputs and existing forced outputs, and removes failed candidates.
Supported layouts keep exact report shapes and every decompressed member byte.
This agreement check does not independently validate directory extents; unreadable
or malformed directories still fail in Python's ZIP reader.

The selected classic EOCD must have its entire declared **archive comment**
present, including when ZIP64 end records precede it. The two-byte declaration
is compared with the bytes available after the fixed 22-byte EOCD through EOF.
For example, a one-byte declaration with no bytes remaining raises
`truncated ZIP archive comment: 'trace.zip'; classic EOCD declared 1 bytes, available 0 bytes`.
This check runs before directory parsing or payload decoding on both comparison
inputs, inspect, repack, dry-run, and completed candidates. The CLI exits **1**
with empty stdout, preserves inputs and existing forced outputs, and removes
invalid completed candidates before publication.

EOCD selection stays unchanged: use the final fixed zero-comment record when
present, otherwise the last signature in the bounded tail search. A truncated
selected comment does not cause a search backward for an earlier EOCD. Fully
present empty, short and 65,535-byte comments remain supported; extra trailing
bytes are permitted within the existing search window, so the declared comment
need not end exactly at EOF. Signatures embedded in comments/trailing data can
affect selection; this is not an expanded recovery or ZIP conformance policy.
Repack still normalizes comments away and preserves every member byte.

Run the comment-boundary and actual candidate-cleanup contracts with:

```sh
uv run --extra test pytest tests/trace_budget/test_zip_end_comments.py -q
```

Run the raw size/offset and candidate-cleanup contracts with:

```sh
uv run --extra test pytest tests/trace_budget/test_zip_end_directory.py -q
```

Run the resolved-total and publication-failure contracts with:

```sh
uv run --extra test pytest tests/trace_budget/test_zip_entry_totals.py -q
```

CRC32 and sizes follow these input rules:

| Local form | Accepted local CRC32 and resolved sizes | Required descriptor |
| --- | --- | --- |
| Flag bit 3 clear | Exact central-directory values | None |
| Flag bit 3 set | Each value is zero or exactly matches the central directory | Matching CRC32 and both sizes immediately after the compressed payload |

Allowing redundant matching values with bit 3 is a compatibility policy; zero
placeholders are the usual streaming form. A descriptor may include or omit the
`0x08074b50` signature. A CRC equal to that signature is handled by checking both
interpretations. Sizes use 32 bits normally and 64 bits for a local ZIP64 entry.
The descriptor cannot extend into the next local header or central directory.

Local ZIP64 requires **both** 32-bit size fields to be `0xffffffff` and exactly
one `0x0001` extra block containing two 64-bit sizes (uncompressed, compressed;
16 bytes total). Those resolved values obey the table above. Missing, duplicate,
short, oversized, contradictory, or unsolicited local ZIP64 blocks fail. All
local extra fields need complete tag/length/data framing; unknown fields remain
opaque. Central ZIP64 sizes are resolved separately by zipfile and need not use
the same raw sentinel representation as the local header.

Central extra areas must also contain only complete tag/length/data fields:
each field needs a four-byte little-endian tag/length header followed by its
declared data bytes. A standalone or trailing 1–3 byte header is rejected for
**every member before any payload decoding**, including completed repack
candidates. For example, central extra bytes `fe`, `fe ca`, or `fe ca 00` on
`resources/body` fail with
`invalid ZIP central extra: 'trace.zip'; 'resources/body'; truncated field header`.
Empty extra areas, complete unknown tags, zero-length fields (such as
`fe ca 00 00`) and repeated unknown fields remain supported. Their contents are
opaque. Central ZIP64 retains zipfile's conditional uncompressed-size,
compressed-size and local-offset layout; the local fixed two-size policy does
not apply to it. zipfile may reject truncated field data or unreadable ZIP64
metadata earlier with its generic ZIP diagnostic. This framing check adds no
new field-content semantics.

As a conservative **tool compatibility policy**, each member may have at most
one central `0x0001` ZIP64 extra block. Repeated blocks are rejected even when
identical or separated by complete unknown fields. This uniqueness rule is
separate from TLV framing and applies before any member payload is decoded,
including completed repack candidates. The diagnostic names archive and member:
`invalid ZIP central ZIP64: 'trace.zip'; 'resources/body'; duplicate block`.
Single conditional size-only, compressed-size-only, offset-only and combined
blocks remain supported, as do repeated unknown and zero-length unknown fields.
This rule imposes no new conditional ZIP64 data-length policy and does not
require the local two-size layout in central fields.

These checks apply to `inspect`, `compare` (both inputs), `repack`, and
`repack --dry-run`. A mismatch raises `ArchiveError` in the library; the CLI exits
**1**, names the member on stderr, emits no success JSON, and preserves existing
reports/archives even with `--force`. For example, changing only the local method
of `resources/body` to 99 fails with
`local ZIP method mismatch: 'resources/body'`, even if the payload and central
directory are intact. Valid descriptor/ZIP64 inputs retain the ordinary report
shape and repack with every payload unchanged.

The format reference is [PKWARE APPNOTE 6.3.10](https://pkware.cachefly.net/webdocs/casestudies/APPNOTE.TXT),
sections 4.3.9, 4.3.14–16, 4.4.3–4, 4.4.7–9, 4.4.13, and 4.5.3. This is a consistency check for supported
members and end requirements, not a complete ZIP conformance audit: timestamps,
unrelated extra-field semantics, and
padding/unindexed records are not audited. The version ceiling and zero-high-byte
rule are this tool's
conservative compatibility policy. zipfile may reject unreadable central metadata
before per-member checks (for example, central extraction version 99); these
errors still fail but may use a generic ZIP diagnostic without a member name.
Descriptor bounds use zipfile's per-member next-record offsets.

Run the small synthetic contract matrix with:

```sh
uv run --extra test pytest tests/trace_budget/test_zip*.py -q
```

| Policy | Default | CLI flag |
| --- | ---: | --- |
| Archive file | 512 MiB | `--max-archive-bytes` |
| Members | 10,000 | `--max-members` |
| Each uncompressed member | 512 MiB | `--max-member-bytes` |
| Total uncompressed members | 2 GiB | `--max-total-bytes` |
| Each uncompressed/compressed size ratio | 1,000 | `--max-ratio` |
| Each NDJSON line, excluding newline | 8 MiB | `--max-line-bytes` |

All limits must be finite and positive. Declared sizes are checked before reading
payloads, and streamed byte/line limits are enforced during both validation and
copying. Raw DEFLATE decoding produces at most 128 KiB per chunk, reduced near
member, total, or declared-size boundaries to at most one byte past the boundary
so a violation can be detected before hashing or writing that chunk. An
underdeclared size is rejected rather than silently clipping decoded output.
Python `zipfile` loads the
central directory into memory before the member-count check; this is not a hard
process-memory cap. Larger inputs require explicit limits and adequate memory.
The candidate output must pass the same limits, including its compression ratio;
raise a limit explicitly if stronger compression crosses that policy. A budget
is a separate measurement policy and does not weaken validation limits.

Only synthetic format-envelope fixtures have been tested. No browser, real trace
capture, or viewer was run; viewer compatibility, older trace modernizers, full
semantic validity, future schema changes, adoption and real-world gains remain
unverified. The official [Playwright viewer and recording controls](https://playwright.dev/docs/trace-viewer)
remain the upstream facilities.

## Tests and reproducible comparison

From a qawrench checkout:

```sh
uv run --extra test pytest tests/trace_budget -q
python3 examples/trace_budget/benchmark.py --out /tmp/trace-benchmark.json
```

The tests use only local synthetic files. They cover full CLI paths, CRC/SHA
equality, screenshot resource preservation, multiple streams, zero-gain and
expanding outputs, budget failures, malformed ZIP/NDJSON, encrypted flags,
unsafe/duplicate names, limits, aliasing, output races, digest mismatch,
concurrent source changes, atomic cleanup, and dry-run no-write behavior.
Comparison regressions cover category/overhead reconciliation, exact event-line
bytes across read boundaries, multi-stream loss, empty networks, retained SHA
manifests, removal opt-in, changed/added member rejection, before/after budget
boundaries, and protection of both input paths.
Payload regressions also cover incomplete DEFLATE with a matching CRC, forged
zero/short sizes, trailing/concatenated streams, short decoded payloads, exact
size/chunk boundaries, named CRC diagnostics, bounded expansion of a forged
4 MiB resource at a 1,024-byte limit, and valid descriptor/ZIP64 local headers.
Local-header regressions independently encode STORED/DEFLATED entries, empty
payloads, signed/unsigned descriptors, descriptor-signature CRC collisions,
zero/redundant local values, central ZIP64 resolution, malformed local extras,
and missing/truncated/contradictory descriptors at both next-record boundaries.
They check inspection, both comparison inputs, and forced repack failures while
preserving input/destination bytes and leaving no temporary files. Extraction
version regressions cover local 99/central 20, both-byte disagreement, unsupported
matching requirements, method/directory/ZIP64 minima, and matching higher supported
versions. They exercise CLI dry-run and empty failure stdout, and corrupt a real
completed candidate to verify temporary cleanup before publication.
Directory declaration regressions reject lower/higher classic literals, combined
contradictions, unresolved sentinels, and 64-bit mismatches before decoding either
comparison input. They cover raw offsets with both prefix coordinate systems,
maximum comments, and invalid completed candidates with absent or forced
destinations.
Central extra-field regressions cover each retained 1–3 byte header, standalone
and trailing, across all operations and both comparison directions, with guarded
rejection before decoding, absent/forced outputs and physical cleanup of completed
candidates. Central ZIP64 uniqueness regressions cover identical and conflicting
duplicates, adjacent and separated by unknown fields, rejected before any payload
decoding; single conditional central ZIP64 layouts and repeated unknown/zero-length
fields remain supported. Run both central-extra contracts with
`uv run --extra test pytest tests/trace_budget/test_zip_central*.py -q`.

Comment-boundary regressions cover classic/ZIP64 zero/partial available bytes,
one-byte-short and maximal declarations, both prefix coordinate systems,
unchanged EOCD selection, fully present comments and permitted trailing data.
ZIP64 end-version regressions distinguish extraction requirements from made-by
and member versions, rejecting low, above-supported and nonzero-high-byte
requirements before decoding either comparison input.
Flag regressions cover every unsupported bit on each method, combined masks,
matching masked headers, all DEFLATE options, UTF-8/CP437 names, ordinary and
ZIP64 signed/unsigned descriptors, report equality, empty CLI failure stdout,
and cleanup of an actual candidate corrupted before its verification scan.
Disk regressions cover member and archive disk declarations, ZIP64 sentinels,
classic fields masked by ZIP64 ends, locator disk counts, maximum-length comments,
prepended data, validation before decoding, and cleanup of real completed invalid
candidates. Count regressions cover classic and ZIP64 per-disk/total mismatches,
contradictory classic literals, unresolved sentinels, mixed literal/sentinel
controls and 64-bit mismatch values.
Resource-role regressions independently reconcile STORED/DEFLATED totals and
exclusive memberships, including repeated and ambiguous references, all three
roles on one member, unknown reference keys, malformed/missing references,
multi-stream/chunk boundaries, stream-removal reclassification, and unchanged
retained-payload parity. All fixtures remain synthetic.

The benchmark compares three generated archives to a level-9 DEFLATE repack and
independently checks CRCs and every member SHA before/after. The baseline stores
text-heavy and incompressible fixtures without compression; the precompressed
case uses DEFLATE already, with a zlib-compressed random resource. Byte totals
and elapsed times are actual local measurements, not estimates. With `--out`, the
benchmark writes a JSON record of all member hashes, generation/hash/repack times,
Python, platform, and zlib versions. Timings are single-run observations and not
statistically significant.

Recorded on CPython 3.14.4 / zlib 1.3.1:

| Synthetic fixture | Original bytes | Repacked bytes | Saved bytes | Saved % | Repack ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Stored text-heavy | 575,549 | 15,515 | 560,034 | 97.3043% | 19.76 |
| Precompressed binary | 2,099,565 | 2,099,565 | 0 | 0% | 40.14 |
| Incompressible binary | 2,098,623 | 2,098,919 | **−296** | **−0.0141%** | 38.46 |

These synthetic results demonstrate the comparison and preservation checks;
they do **not** prove improvements on real Playwright recordings. Long traces
often already contain compressed data; measuring zero/negative gain is useful.

License: MIT for this implementation; see [LICENSE](../LICENSE). See
[THIRD_PARTY.md](../THIRD_PARTY.md) for upstream format licensing.
