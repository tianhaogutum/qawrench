# qawrench frontier

`qawrench frontier` (formerly `crawl-frontier-audit`) compares crawl request
identity policies offline before you build a frontier. It reports every collision,
retains original representatives, and always keeps methods and exact bodies in the
request key. It never fetches URLs or runs request contents.

[w3lib](https://github.com/scrapy/w3lib) already canonicalizes URLs;
[Crawlee](https://crawlee.dev/js/api/core/interface/RequestOptions) already
supports request deduplication, including extended method/payload keys. This
tool adds conservative-versus-proposed policy simulation, complete witnesses,
explicit merge refusal, and labeled offline comparisons. It is not a crawler
or a replacement scheduler. See [tested upstream support](frontier/upstream-support.md).

The companion subcommand `qawrench frontier verify-witnesses` (formerly the separate
`verify-robots-witnesses` script) re-checks saved strict robots witnesses in a report
against local snapshots; see [Verifying saved witnesses from the command line](#verifying-saved-witnesses-from-the-command-line).

## Install

Requires Python 3.11 or newer. This command needs [w3lib](https://github.com/scrapy/w3lib)
(no transitive dependencies), installed via the `frontier` extra; it was tested with
w3lib 2.3.1 on Python 3.12 on Linux.

```sh
pipx install 'qawrench[frontier]'     # or: pip install 'qawrench[frontier]'
qawrench frontier --version           # prints the bare version, e.g. 0.1.0

# Or run without installing:
uvx --from 'qawrench[frontier]' qawrench frontier --help
```

`python -m qawrench frontier` is equivalent to `qawrench frontier`. Without w3lib
installed, the command prints an install hint for the extra and exits.
Installation may access a package index. **Runtime is entirely offline**.

The example inputs below live in `examples/frontier/` in the qawrench repository;
run the commands from the repository root (or adjust the paths).

## Working examples

All supplied inputs and labels are **synthetic**, using reserved `.test` domains.
The sample policy permits its merges for demonstration; it is not a recommended
policy for any real site.

```sh
qawrench frontier examples/frontier/requests.jsonl \
  --policy examples/frontier/policy.json \
  --frontier /tmp/frontier-frontier.jsonl --report /tmp/frontier-report.json

# Conservative exact baseline, with no normalization policy:
qawrench frontier examples/frontier/requests.jsonl \
  --frontier /tmp/frontier-exact-frontier.jsonl --report /tmp/frontier-exact-report.json

# Inspect complete witnesses without writing anything, even when paths are supplied:
qawrench frontier examples/frontier/requests.jsonl \
  --policy examples/frontier/sort-policy.json --dry-run

# Local JSON representation of Crawlee's documented sources array subset:
qawrench frontier examples/frontier/crawlee-sources.json \
  --format crawlee --frontier /tmp/frontier-crawlee-frontier.jsonl \
  --report /tmp/frontier-crawlee-report.json
```

The first command reads 11 valid requests: 9 distinct URL strings, 10 exact
request keys, and 8 proposed keys; it retains 8 representatives and identifies
3 duplicate occurrences. Reordered `tag` parameters and all three distinct
POST bodies stay separate. Warnings identify method/body differences at a
shared proposed URL and differing provenance in duplicate groups.

## Checkpointed batch merge

Save batch A, then merge batch B with its saved policy and complete history:

```sh
qawrench frontier examples/frontier/merge-batch-a.jsonl \
  --batch-id seed:a --checkpoint-out /tmp/frontier-batch-a.checkpoint.json \
  --frontier /tmp/frontier-batch-a.frontier.jsonl --report /tmp/frontier-batch-a.report.json
qawrench frontier examples/frontier/merge-batch-b.jsonl \
  --batch-id seed:b --checkpoint-in /tmp/frontier-batch-a.checkpoint.json \
  --checkpoint-out /tmp/frontier-batch-b.checkpoint.json \
  --frontier /tmp/frontier-merged.frontier.jsonl --report /tmp/frontier-merged.report.json
```

Expected: A retains GET with empty body and POST `a=1`. The merged frontier
retains those first originals, then POST `a=2`: **3 representatives from 4
records**. The duplicate POST `a=1` has witnesses at `batch 1 line 2` and
`batch 2 line 1`, including both source labels. Methods and exact bodies always
remain in identity. Counts and witnesses cover the entire combined history;
indices are cumulative across raw records, including invalid records.

Omitting `--policy` inherits the checkpoint policy. An explicit policy must
match every saved field, including merge permission; only the order of
`remove_query_keys` is irrelevant. For example, adding
`--policy examples/frontier/sort-policy.json` to the second command exits **2**, writes
a rejected report with each differing field's `checkpoint` and `requested`
values in `checkpoint.policy_conflicts`, and leaves the frontier and next
checkpoint untouched. Counts then describe the candidate under the requested
policy, identified by `report.policy`; it is never emitted. Matching policies
still undergo normal ambiguity checks: a collision first introduced across
batch boundaries is refused unless explicitly permitted by the saved policy.

Use **`--batch-id`** to make a batch retry idempotent:

```sh
# Retry B against the checkpoint that already includes B: still 4 records,
# 3 representatives, 2 batches, and the same checkpoint bytes.
qawrench frontier examples/frontier/merge-batch-b.jsonl \
  --batch-id seed:b --checkpoint-in /tmp/frontier-batch-b.checkpoint.json \
  --checkpoint-out /tmp/frontier-replayed.checkpoint.json \
  --frontier /tmp/frontier-merged.frontier.jsonl --report /tmp/frontier-replayed.report.json

# Expected exit 2: A differs from the text already registered as seed:b.
# Existing frontier and replayed checkpoint remain unchanged; report is replaced.
qawrench frontier examples/frontier/merge-batch-a.jsonl \
  --batch-id seed:b --checkpoint-in /tmp/frontier-batch-b.checkpoint.json \
  --checkpoint-out /tmp/frontier-replayed.checkpoint.json \
  --frontier /tmp/frontier-merged.frontier.jsonl --report /tmp/frontier-conflict.report.json
```

An ID is a case-sensitive ASCII token of 1–128 characters matching
`[A-Za-z0-9][A-Za-z0-9._:-]*`, scoped to one checkpoint history. It requires
`--checkpoint-in` or `--checkpoint-out`. The first occurrence appends; repeating
the ID with the same **format and exact UTF-8 text** is a history no-op. Counts,
indices, witnesses and serialized state stay unchanged. Filenames do not
participate. Whitespace, JSON key order, provenance, bodies and line endings
do participate; semantically similar input can still conflict. Comparison uses
full text, not a hash. A different ID intentionally appends identical text;
omitting the ID always appends an anonymous occurrence. IDs are not inferred
from old content or retroactively assigned to anonymous batches.

`report.checkpoint.incoming_batch` records `batch_id` (null for anonymous) and
`status`: `appended`, `replayed`, or `conflict`. An ID conflict exits **2** with
a rejected report and `checkpoint.batch_conflicts` containing the ID and both
`checkpoint`/`requested` format and `text_sha256` witnesses. No conflicting
records enter state; counts/witnesses describe **saved history only**, under
`report.policy`. Frontier and checkpoint outputs stay byte-for-byte unchanged
or absent; only the report is written. `--dry-run` prints the report and writes
nothing. Successful replay still re-audits saved history under current options:
policy conflicts, historical invalid records and current robots enforcement
can reject it. Accepted runs may regenerate frontier/checkpoint/report files;
the no-op applies to history, not to all file I/O.

Use **`--checkpoint-only`** to re-audit saved history without supplying a new
request file or a stable batch ID:

```sh
qawrench frontier --checkpoint-only \
  --checkpoint-in /tmp/frontier-batch-b.checkpoint.json \
  --checkpoint-out /tmp/frontier-reaudited.checkpoint.json \
  --frontier /tmp/frontier-reaudited.frontier.jsonl --report /tmp/frontier-reaudited.report.json

# Inspect the same history under current robots enforcement without any writes:
qawrench frontier --checkpoint-only \
  --checkpoint-in /tmp/frontier-batch-b.checkpoint.json \
  --robots examples/frontier/robots-snapshots.json --user-agent AuditBot \
  --enforce-robots --dry-run
```

The first command still audits **4 records in 2 batches**, retaining all IDs,
original text, indices, representatives and collision witnesses. No dummy or
empty batch is appended. `checkpoint.incoming_batch` is
`{"batch_id": null, "status": "not_supplied"}`; `batches` and `previous_batches`
are equal. `--checkpoint-out` is optional. The second command exits 0 because
all four saved `/submit` requests are allowed by the supplied snapshot, and
prints the audit without writing files.

This mode requires `--checkpoint-in` and refuses a positional input,
`--batch-id`, or explicit `--format` before reading files or writing outputs.
Without this flag, a positional input remains required. Version-1 anonymous
and version-2 named histories are supported. Checkpoint output uses the same
canonical serialization as merge/replay: generated checkpoints retain their
bytes, arbitrary input JSON whitespace is not preserved, and version-2
anonymous-only history normalizes to version 1 as described below.

Saved invalid/unsupported records still require explicit `--on-invalid skip`.
Current policy conflicts, ambiguous merges, robots enforcement and strict
malformed-rule checks can all reject a re-audit. A rejection writes only the
requested report and preserves frontier/checkpoint bytes or absence.
`--dry-run` prints either accepted or rejected reports and writes nothing.
Snapshot and enforcement options must be supplied on each run.

The strict version-1 checkpoint is a JSON object with exactly `version: 1`,
the complete `policy` object, and a nonempty `batches` array. Each batch has
exactly `format` (`jsonl` or `crawlee`) and `text` (the original UTF-8 input
text snapshot). Version 1 remains readable and is emitted for anonymous-only
history. Once a named batch is present, version **2** is emitted: the same
top-level fields, with exactly `format`, `text`, and `batch_id` in every batch.
Anonymous entries have `batch_id: null`. Non-null IDs must be valid and unique;
duplicate IDs in saved state are rejected even when their content is identical.
Old versions of this tool cannot read version-2 checkpoints. Version-2 input
containing only anonymous entries serializes back to version 1. All originals,
previously collapsed members, metadata and
invalid records are replayed; a frontier-only snapshot would lose witnesses.
Inputs can mix the two supported formats. No saved filename is opened and
no saved fingerprint is trusted as identity. Unknown fields/versions,
incomplete policies, malformed checkpoint JSON or broken Crawlee envelopes
fail before writing any output. JSONL issues follow `--on-invalid`, which
must explicitly be `skip` on each run that includes them.

`report.checkpoint` records the candidate checkpoint version, total/previous
batch counts, policy and batch conflicts, the incoming batch outcome (or
`not_supplied` in checkpoint-only mode),
and whether a new checkpoint was emitted. `--checkpoint-in` also works without
`--checkpoint-out` for a one-off merge or checkpoint-only audit. `--dry-run` writes neither state nor
frontier/report, even when output paths are supplied. Checkpoint inputs and
outputs have the same alias/hardlink/symlink protection as other files; use
a **different next checkpoint path**, since in-place checkpoint updates are
refused. Writes occur in frontier, checkpoint, report order, each atomically;
the three files are **not a transaction**. An I/O failure after an earlier
replacement can leave those earlier outputs changed; check exit status.

This is an in-memory, whole-batch frontier merge, not a scheduler or a mid-batch
cursor. Every merge re-audits the full history, and checkpoints grow with that
history; no speedup is claimed. Appending without IDs adds duplicate input
occurrences even though request deduplication keeps the same frontier. IDs do
not provide cross-file transactions, concurrent-writer coordination or a
global registry. Legacy checkpoints retain the text previously saved; line
endings normalized by older versions cannot be recovered.
Robots snapshots are not checkpointed; when supplied on a merge, the current
snapshots audit every original in every batch. Prior robots decisions are
not inherited when those options are omitted. Robots enforcement is also
explicit on each invocation; checkpoints do not save the enforcement mode.

## Offline robots.txt audit

Supply local snapshots and an explicit crawler **product token**:

```sh
qawrench frontier examples/frontier/robots-requests.jsonl \
  --robots examples/frontier/robots-snapshots.json --user-agent AuditBot \
  --frontier /tmp/frontier-robots-frontier.jsonl --report /tmp/frontier-robots-report.json
```

This synthetic example retains all four requests and reports one `allow`, one
`disallow`, and two `unknown` decisions. `/private/x` is disallowed by line 5;
`/private/public` is allowed by the more specific line 6. The HTTP origin has
no supplied snapshot (`status: "unknown"`); `https://down.test` is explicitly
`unavailable` with reason `snapshot not captured`. Neither becomes an inferred
allow. The exit code is 0 because this is an **informational audit**: robots
findings produce warnings, do not filter the frontier, and do not override
existing deduplication acceptance rules. `--dry-run` prints the same audit
without writing files.

Add **`--enforce-robots`** to require every valid original request to have an
`allow` decision before emitting any frontier or checkpoint:

```sh
# Expected exit 2: one disallow and two unknown decisions in this fixture.
qawrench frontier examples/frontier/robots-requests.jsonl \
  --robots examples/frontier/robots-snapshots.json --user-agent AuditBot --enforce-robots \
  --frontier /tmp/frontier-robots-frontier.jsonl --report /tmp/frontier-robots-report.json

# All-allow example using the fixture's second original request:
sed -n '2p' examples/frontier/robots-requests.jsonl > /tmp/frontier-robots-allowed.jsonl
qawrench frontier /tmp/frontier-robots-allowed.jsonl \
  --robots examples/frontier/robots-snapshots.json --user-agent AuditBot --enforce-robots \
  --frontier /tmp/frontier-robots-allowed-frontier.jsonl --report /tmp/frontier-robots-allowed-report.json
```

The first command rejects the **whole run**, writes a report with
`status: "rejected"`, `frontier_emitted: false` and
`robots.mode: "offline-enforced"`, and leaves the existing frontier byte-for-byte
unchanged (or absent if none existed). Any next checkpoint is also preserved;
`checkpoint.emitted` stays false. The full robots audit, rule/source witnesses,
collision witnesses and candidate counts remain in the report, including
`counts.accepted: 4`; this count does **not** mean requests were emitted.
The second command exits 0 and emits one original representative, provided
the other input/deduplication checks pass.

Enforcement checks **all valid original URLs**, including duplicates, records
collapsed by permitted normalization, and every original in checkpoint history.
Absent snapshots and explicit `unavailable` snapshots are both `unknown` and
reject the run. Neither `--on-invalid skip` nor `allow_ambiguous_merges` bypasses
this gate; the ordinary invalid-record and ambiguity checks still apply.
`--enforce-robots` requires both `--robots` and `--user-agent`; missing/invalid
options or malformed manifests fail before any output write. With `--dry-run`,
the same rejection exits 2 and prints the report to stdout without writing any
files. Omitting the flag preserves the informational behavior and
`robots.mode: "offline-informational"`.

Add **`--strict-robots`** alongside `--enforce-robots` to reject malformed-rule
diagnostics even when tolerant matching allows every request:

```sh
sed -n '2p' examples/frontier/robots-requests.jsonl > /tmp/frontier-robots-strict-input.jsonl
# Expected exit 2: invalid path pattern at snapshot line 2, despite an allow decision.
qawrench frontier /tmp/frontier-robots-strict-input.jsonl \
  --robots examples/frontier/robots-malformed-snapshots.json --user-agent AuditBot \
  --enforce-robots --strict-robots \
  --checkpoint-out /tmp/frontier-robots-strict-checkpoint.json \
  --frontier /tmp/frontier-robots-strict-frontier.jsonl --report /tmp/frontier-robots-strict-report.json
```

Strict mode requires `--enforce-robots` and its manifest/token inputs; it is never
inherited from checkpoints. The default informational audit and default enforced
matching remain tolerant. Strict mode adds `robots.strict: true` and
`robots.malformed_rules`, listing `origin`, `source`, `line`, original `text`,
and `reason` for each blocking diagnostic. Reasons are **`invalid path pattern`**,
**`invalid user-agent token`**, **`rule outside a valid group`**, and
**`missing colon after core directive`**. Original
per-origin diagnostics, selected groups, request decisions, rule witnesses and
candidate counts remain intact. As with ordinary enforcement rejection, exit 2
writes the rejected report and preserves frontier/checkpoint bytes or absence;
`--dry-run` only prints the report and writes no files.

For example, a colonless `Disallow /private` is ignored by tolerant matching but
rejects strict emission, even when the request decision is `allow`:

```sh
sed -n '2p' examples/frontier/robots-requests.jsonl > /tmp/frontier-robots-colonless-input.jsonl
# Expected exit 2, malformed_rules line 2, reason "missing colon after core directive".
qawrench frontier /tmp/frontier-robots-colonless-input.jsonl \
  --robots examples/frontier/robots-colonless-snapshots.json --user-agent AuditBot \
  --enforce-robots --strict-robots \
  --checkpoint-out /tmp/frontier-robots-colonless-checkpoint.json \
  --frontier /tmp/frontier-robots-colonless-frontier.jsonl --report /tmp/frontier-robots-colonless-report.json
```

Recognition strips inline `#` comments and surrounding ASCII spaces/tabs, then
checks an exact ASCII case-insensitive `User-agent`, `Allow` or `Disallow`
followed by a space/tab or the end of the record. Bare names and comment-only
values also require a colon. A colon immediately after the field, with optional
spaces/tabs, is valid; a colon later in the value (`Disallow /a:b`) does not repair
the delimiter. Prefixes/suffixes such as `Disallow-extra` and `Allowance` are
extensions. Strict witnesses retain original line text, whitespace, comments and
source; per-origin diagnostics still say `ignored non-rule record`, and the
matcher does not reinterpret the malformed line. Omitting `--strict-robots`
preserves the tolerant reports and matching. The same gate applies to
`--checkpoint-only`, named replay and appended history.
A single initial U+FEFF BOM is accepted as a signature. Strict `malformed_rules`
witnesses retain it in the exact original first record, along with whitespace
and comments; CR, LF and CRLF are separators and are excluded from witness text.
For compatibility, tolerant per-origin `diagnostics` still omit that initial
signature, including within strict reports. Default informational/enforced
reports and matching are unchanged. A second initial BOM or a BOM elsewhere
is ordinary record data; it is not stripped to recognize a core directive.

For example, this snapshot starts with a BOM-prefixed colonless `Disallow`:

```sh
sed -n '2p' examples/frontier/robots-requests.jsonl > /tmp/frontier-robots-bom-input.jsonl
# Expected exit 2; malformed_rules[0].text starts with U+FEFF (JSON: \ufeff).
qawrench frontier /tmp/frontier-robots-bom-input.jsonl \
  --robots examples/frontier/robots-bom-snapshots.json --user-agent AuditBot \
  --enforce-robots --strict-robots --dry-run
```

The strict witness is `"\ufeffDisallow /private # original first record"` at
line 1 with source `synthetic:bom-witness`. The request still has an `allow`
decision, and the tolerant diagnostic omits the BOM. The same witness and
rejection apply when re-auditing saved history with `--checkpoint-only`.

Each strict `malformed_rules` witness also has `snapshot_sha256`, `byte_start`
and `byte_end`. The digest equals the encountered origin's `sha256`: SHA-256
of the **entire supplied snapshot text encoded as UTF-8**, before BOM removal
or newline normalization. Offsets are zero-based UTF-8 byte positions in that
same text, with an inclusive start and exclusive end. The range includes the
original record's BOM, whitespace and comments, and excludes CR/LF separators;
CRLF counts as two bytes and one separator. Repeated identical records have
distinct ranges. A final record without a newline ends at the snapshot's byte
length. These fields appear only in strict malformed-rule witnesses; tolerant
diagnostics and default informational/enforced reports retain their shape.

For example, this synthetic fixture has mixed separators and repeated records:

```sh
# Expected exit 2; prints the rejected report without writing any files.
qawrench frontier /tmp/frontier-robots-bom-input.jsonl \
  --robots examples/frontier/robots-byte-ranges-snapshots.json --user-agent AuditBot \
  --enforce-robots --strict-robots --dry-run > /tmp/frontier-robots-byte-report.json
```

Its first malformed record has range `[0, 33)` (including the three-byte BOM);
the identical malformed records at lines 5 and 6 have ranges `[95, 124)` and
`[125, 154)`. The exported offline library helper
`verify_malformed_rule_witness(witness, manifest, *, verify_reason=False)` checks a witness against the
caller-retained version-1 manifest. After installing the package and running the
CLI example above (expected exit 2), run this library example:

```sh
python -I - <<'PY'
import json
from pathlib import Path
from qawrench.frontier import verify_malformed_rule_witness

manifest = json.loads(Path("examples/frontier/robots-byte-ranges-snapshots.json").read_text())
report = json.loads(Path("/tmp/frontier-robots-byte-report.json").read_text())
for witness in report["robots"]["malformed_rules"]:
    result = verify_malformed_rule_witness(witness, manifest)
    assert result == {"verified": True, "failure": None}, result

# Equal text at another occurrence does not repair an inconsistent location.
first, second = report["robots"]["malformed_rules"][1:3]
altered = {**first, "byte_start": second["byte_start"], "byte_end": second["byte_end"]}
failure = verify_malformed_rule_witness(altered, manifest)
assert failure["verified"] is False
assert failure["failure"]["code"] == "line_mismatch"
print("verified supplied-content provenance; altered repeated-record location rejected")
PY
```

The helper accepts two JSON-decoded objects, with no file/path or
`RobotsPolicy` overload. It returns `{"verified": True, "failure": None}` on
success. On failure it returns `{"verified": False, "failure": {"code": ...,
"field": ..., "message": ...}}`. Codes and field names are machine-readable;
messages are explanatory text. `verify_reason` is keyword-only and must be a
boolean; other values return `invalid_option` before validating either input.
It then validates the entire manifest using
`RobotsPolicy.from_dict`, including unused origins, before checking the witness.
Manifest origin keys may use accepted aliases; witness origins must use the
emitted canonical spelling. It requires exactly the eight emitted fields:
`origin`, `source`, `line`, `text`, `reason`, `snapshot_sha256`, `byte_start`,
`byte_end`. String fields must contain valid UTF-8 Unicode, `source` must be
nonblank, `reason` must name one of the four strict categories above, the digest
must be 64 lowercase hexadecimal characters, and `line` must be a positive
integer. Integer checks exclude booleans. Legacy witnesses without ranges are
rejected rather than repaired.

The first failing check is returned in this order:

| Code | Meaning / `field` |
| --- | --- |
| `invalid_option` | `verify_reason` is not a boolean / `verify_reason` |
| `invalid_manifest` | Existing version-1 validation failed / `manifest` |
| `invalid_witness` | Wrong shape / `witness`; invalid schema value / its field |
| `missing_origin` | Canonical origin absent from manifest / `origin` |
| `unavailable_snapshot` | Origin has no available text / `origin` |
| `source_mismatch` | Exact source label differs / `source` |
| `digest_mismatch` | Whole-snapshot SHA-256 differs / `snapshot_sha256` |
| `invalid_span` | Offsets are not integers or fail `0 <= start < end <= byte length` / `byte_start/byte_end` |
| `record_mismatch` | Range is not exactly one complete record, excluding separators / `byte_start/byte_end` |
| `text_mismatch` | Selected original record differs / `text` |
| `line_mismatch` | Selected record's one-based line number differs / `line` |
| `not_malformed_rule` | Opt-in only: selected record has no blocking strict classification / `reason` |
| `reason_mismatch` | Opt-in only: selected record has a different blocking strict reason / `reason` |

Verification selects the exact span before comparing text and line; it never
searches for equal text elsewhere. BOMs, multibyte text, mixed CR/LF/CRLF,
repeated identical records and a final record without a newline are supported.
The helper performs no fetching, file I/O, audit emission or checkpoint changes,
and does not mutate either input. Success establishes **supplied-content
consistency only**. By default (`verify_reason=False` or omitted), it does not
reclassify the record or establish that its `reason` is correct. A recognized
but incorrect reason, or a consistent full-record witness for a valid rule or
ignored extension, can pass provenance-only verification.

Pass **`verify_reason=True`** to additionally confirm the claimed blocking
strict reason. All provenance checks run first with their existing failures.
The helper then reuses the existing parser and strict classifier on the whole
bound origin snapshot, including all groups, to retain preceding group context.
It selects the classification by the same exact byte range and line, never by
equal text elsewhere. No product token is needed: strict malformed records in
any group count, even when a request audit would select another group.
The four supported categories are unchanged. A valid rule, empty rule, comment
or ignored extension returns `not_malformed_rule`; a different recognized
blocking reason returns `reason_mismatch`. An unrecognized claimed reason still
fails earlier with `invalid_witness`. This opt-in mode performs in-memory parsing
but no file I/O, fetching, audit emission or checkpoint work.

After installing the package, this self-contained library example exercises
both modes without reading or writing files:

```sh
python -I - <<'PY'
import hashlib
from qawrench.frontier import verify_malformed_rule_witness

text = "\ufeffUser-agent: Bot\r\nDisallow: bad%xx # é"
manifest = {"version": 1, "origins": {"https://example.test": {
    "status": "available", "source": "synthetic:reason-example", "text": text}}}
record = text.split("\r\n")[1]
witness = {"origin": "https://example.test", "source": "synthetic:reason-example",
    "line": 2, "text": record, "reason": "invalid path pattern",
    "snapshot_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    "byte_start": len("\ufeffUser-agent: Bot\r\n".encode("utf-8")),
    "byte_end": len(text.encode("utf-8"))}
assert verify_malformed_rule_witness(witness, manifest, verify_reason=True) == {
    "verified": True, "failure": None}
altered = {**witness, "reason": "rule outside a valid group"}
assert verify_malformed_rule_witness(altered, manifest)["verified"] is True
failure = verify_malformed_rule_witness(altered, manifest, verify_reason=True)
assert failure["failure"]["code"] == "reason_mismatch"
print("verified supplied classification; incorrect recognized reason rejected only on opt-in")
PY
```

Neither mode establishes that the witness came from an audit or that an audit's
witness list is complete. A caller who changes all fields consistently can
produce another verifiable witness. Source labels and matching hashes provide
no independent authenticity, freshness or live-server guarantee.

For **complete coverage of explicitly supplied snapshots**, use the separate
library helper `verify_malformed_rule_collection(witnesses, manifest, *, origins)`.
It accepts JSON-decoded objects: an explicit witness **list** (empty allowed),
the complete version-1 manifest, and a required keyword-only nonempty **list**
of unique canonical origin strings. No scope is inferred from the witnesses,
requests, report or manifest inventory. Manifest keys may use supported aliases;
scope strings must use canonical spelling (no trailing `/`, default port or
host-case alias). Each scoped origin must have an available snapshot. Unscoped
unavailable snapshots are allowed, but the entire manifest is still validated.
There are no other options or path/JSON-text/`RobotsPolicy` overloads; omitted,
positional or unknown keyword arguments raise Python `TypeError`.

The result always has exactly `complete`, `failure`, `entries` and `missing`.
Envelope failure returns `complete: false`, `{code, field, message}` in
`failure`, and empty lists; **empty lists in a failed envelope say nothing about
coverage**. Validation stops at the first failure, in this order:

| Code | Check / `field` |
| --- | --- |
| `invalid_scope` | Nonempty list / `origins`; invalid, noncanonical or repeated origin in input order / `origins[i]` |
| `invalid_manifest` | Entire version-1 manifest, including unused entries / `manifest` |
| `missing_origin` | First scoped origin absent from manifest, in scope order / `origins[i]` |
| `unavailable_snapshot` | First scoped snapshot unavailable, in scope order / `origins[i]` |
| `invalid_collection` | Submitted collection is not a list / `witnesses` |

For evaluated collections `failure` is null. `entries` preserves every original
zero-based index as `{index, status, failure, duplicate_of}`. Each entry first
uses the unchanged single-witness helper with `verify_reason=True`, including
its closed eight-field schema and failure order. The statuses have this precedence:

- `invalid`: a provenance, schema or strict reason check failed; `failure` is
  the single helper's failure. Invalid entries never cover expected records,
  even when their origin is outside scope or their span repeats another entry.
- `unexpected`: a semantically verified witness is outside scope;
  `failure.code` is `unexpected_origin`, `field` is `origin`. Repeated out-of-scope
  entries remain unexpected. Absent/unavailable manifest origins fail earlier
  as invalid entries because they cannot be verified.
- `duplicate`: a verified in-scope record already covered by an earlier verified
  entry; `failure.code` is `duplicate_witness`, `field` is `witness` and
  `duplicate_of` is that first entry's index. Equal text at another range is a
  different record. Invalid earlier entries do not establish duplicate identity.
- `verified`: a first verified in-scope record, with null `failure`.

`duplicate_of` is null except for duplicates. Messages are explanatory text;
codes, fields and statuses are machine-readable. `missing` contains exact
eight-field witnesses for all uncovered current blocking strict diagnostics,
in **explicit origin-list order, then snapshot record order**, independent of
submission order. Every group participates, without request or product-token
selection. A valid/ignored record or a recognized wrong reason cannot cover a
malformed record. BOMs, full UTF-8 ranges, mixed CR/LF/CRLF, repeated text and EOF
retain the single helper's exact bindings. `complete` is true only when every
submitted entry is verified and `missing` is empty. A complete zero-record
collection is valid; duplicates or unexpected entries still make it false even
when nothing is missing.

After installing the package, run this example without input/output files:

```sh
python -I - <<'PY'
import hashlib
from qawrench.frontier import verify_malformed_rule_collection

origin = "https://example.test"
prefix, record = "\ufeffUser-agent: Bot\r\n", "Disallow: bad%xx # é"
text = prefix + record + "\r" + record  # repeated record, final EOF
manifest = {"version": 1, "origins": {
    origin: {"status": "available", "source": "synthetic:coverage", "text": text},
    "https://empty.test": {"status": "available", "source": "synthetic:empty", "text": ""}}}
size, start = len(record.encode("utf-8")), len(prefix.encode("utf-8"))
witnesses = [{"origin": origin, "source": "synthetic:coverage", "line": line,
    "text": record, "reason": "invalid path pattern",
    "snapshot_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    "byte_start": offset, "byte_end": offset + size}
    for line, offset in ((2, start), (3, start + size + 1))]
assert verify_malformed_rule_collection(witnesses, manifest, origins=[origin])["complete"] is True
omitted = verify_malformed_rule_collection(witnesses[:1], manifest, origins=[origin])
assert omitted["complete"] is False and omitted["missing"] == witnesses[1:]
duplicate = verify_malformed_rule_collection(witnesses + witnesses[:1], manifest, origins=[origin])
assert duplicate["missing"] == [] and duplicate["entries"][2]["duplicate_of"] == 0
assert duplicate["complete"] is False
assert verify_malformed_rule_collection([], manifest, origins=["https://empty.test"])["complete"] is True
print("current strict snapshot coverage verified; omission and duplicate distinguished")
PY
```

Coverage means only the **current four strict categories within the explicitly
scoped supplied snapshots**. It does not establish report-wide/full-audit
completeness, origin selection correctness, full robots syntax validity,
authenticity, freshness or correspondence to live servers. This helper is pure
in-memory operation: no fetching, file I/O, emission, audit replay, checkpoint
changes or input mutation. It revalidates the full manifest and reclassifies
the bound snapshot per submitted entry, plus enumerating scoped snapshots;
there is no streaming, CPU bound or performance claim. Both commands, existing
single-witness defaults and audit reports/gates remain unchanged.

### Verifying saved witnesses from the command line

To verify every retained witness in a saved report without Python glue, install
the package and run the read-only `verify-witnesses` subcommand (using the report from the
byte-range CLI example above):

```sh
qawrench frontier verify-witnesses /tmp/frontier-robots-byte-report.json \
  --robots examples/frontier/robots-byte-ranges-snapshots.json
```

To also verify each claimed strict reason, explicitly add the no-value switch:

```sh
qawrench frontier verify-witnesses /tmp/frontier-robots-byte-report.json \
  --robots examples/frontier/robots-byte-ranges-snapshots.json --verify-reason
```

This example exits 0 for the seven genuine retained witnesses.
Omitting the flag preserves the original provenance-only behavior and exact
output bytes. With it, every entry uses
`verify_malformed_rule_witness(..., verify_reason=True)`: after provenance
succeeds, classify the whole bound snapshot with current strict rules across
all groups. A recognized wrong reason yields `reason_mismatch`; a valid or
ignored record yields `not_malformed_rule`. Earlier provenance/schema failures
keep precedence. Results retain every original index and failure in order.

The grammar is
`qawrench frontier verify-witnesses REPORT.json --robots MANIFEST.json [--verify-reason]`;
`python -m qawrench frontier verify-witnesses` is equivalent. It accepts no audit,
checkpoint, output or user-agent options. Both paths must name local regular
files (input symlinks are allowed), each at most **16 MiB (16,777,216 bytes)**.
Reads are bounded before UTF-8 decoding; FIFOs/devices/directories are rejected.
Both complete files use the existing strict JSON rules: duplicate keys,
nonfinite numbers, invalid UTF-8 and trailing content are errors. The entire
version-1 manifest is validated, including unused origins, even for zero witnesses.
This per-file bound applies only to this command; decoded objects and repeated
helper checks remain in memory, with no streaming or performance guarantee.

The accepted report envelope requires integer `schema_version: 1` (not a
boolean), a `robots` object with `mode: "offline-enforced"`, boolean
`strict: true`, and an explicit `malformed_rules` array. Other report/robots
fields are allowed and opaque: this command does not validate full audit
semantics, counts, status or policy. Missing/non-array collections are errors;
legacy witnesses without byte/digest fields fail verification. Every original
array entry, including malformed entries and duplicates, is passed unchanged to
`verify_malformed_rule_witness` in report order. It never filters, stops at the
first mismatch, or looks for a replacement record.

Stdout is one compact, sorted-key ASCII JSON line with a final newline. Without
the flag, for an empty array the exact result is:

```json
{"counts":{"failed":0,"submitted":0,"verified":0},"results":[],"schema_version":1,"scope":"submitted-witnesses-only"}
```

With `--verify-reason`, the same envelope additionally carries
`"verification_scope": "provenance-and-strict-reason"`, identifying the requested
checks even when no entries were submitted. The exact empty result (exit 0) is:

```json
{"counts":{"failed":0,"submitted":0,"verified":0},"results":[],"schema_version":1,"scope":"submitted-witnesses-only","verification_scope":"provenance-and-strict-reason"}
```

The historical `scope` still limits both modes to submitted witnesses;
`verification_scope` is absent in the original provenance-only output.
For nonempty arrays, `results` has one object per submitted entry:
`{"index": 0, "verified": true, "failure": null}` for success, or the same
zero-based original `index` plus the helper's unchanged `verified: false` and
`failure` object. `counts.submitted`, `counts.verified` and `counts.failed`
count entries, including repeats. All checks finish before emitting JSON.
Exit **0** means every submitted entry verifies; exit **1** means at least one
witness failed, including malformed witness schema, with all indexed results
retained. Both have empty stderr. Exit **2** means invalid usage, file,
UTF-8/JSON, report envelope or manifest; stdout is empty and stderr contains
usage or `error: ...` diagnostics (diagnostic wording is not a stable API).
`--help` prints usage and exits 0. `--verify-reason` may appear before or after
the paths; repeating it is idempotent. It accepts no value: `--verify-reason=true`,
`--verify-reason false`, abbreviations and `--no-verify-reason` are usage errors.

An empty list means **zero submitted witnesses**, not certification that an
audit found every malformed rule. Neither a successful nonempty result nor the
report envelope establishes full-report authenticity, completeness, source
trust or freshness. Classification correctness under the current strict parser is
checked only with `--verify-reason`; neither mode proves report completeness or
full RFC syntax validity. Both check against the explicitly supplied manifest.
The command performs no audit replay, fetching, source-label opens, checkpoint
changes or output writes;
input bytes/mtimes and directory inventory remain unchanged. Existing
audit/merge/checkpoint commands and library semantics are unchanged.

### Witness ranges, strict gate and manifest details

Ranges address decoded snapshot text, **not bytes in the JSON manifest file**,
its escape spelling, a fetched resource, or normalized parser text. Unicode
line separators and interior BOMs remain ordinary data. The digest identifies
supplied content; it proves neither authenticity nor freshness and retrieves
nothing. To verify a witness, retain the exact supplied text without newline
conversion. Checkpoints do not store snapshots or these witnesses; a
checkpoint-only audit binds witnesses to the snapshots supplied on that run.

The gate checks every group in each **encountered origin**, including groups
for other agents. Unused manifest origins are omitted. It does not reject
other `ignored non-rule record` diagnostics: `Sitemap`, `Crawl-delay`, unknown
extensions and other records the tolerant parser ignores remain nonblocking.
Extensions do not split groups or disable valid rules. Empty policies and
unmatched groups still allow; strict mode is a gate on these four malformed-rule
categories, not a complete robots syntax validator or snapshot freshness check.
All ordinary decision, invalid-request and ambiguity gates still apply.

The version-1 JSON manifest contains inline text, never remote sources:

```json
{
  "version": 1,
  "origins": {
    "https://example.test": {
      "status": "available",
      "source": "synthetic:local-export",
      "text": "User-agent: AuditBot\nDisallow: /private\nAllow: /private/public\n"
    },
    "https://down.test": {
      "status": "unavailable",
      "source": "synthetic:missing-export",
      "reason": "snapshot not captured"
    }
  }
}
```

Both CLI options are required together. A product token contains only ASCII
letters, `_`, or `-`, as in RFC 9309; pass `AuditBot`, not an HTTP header such as
`AuditBot/1.0` or `*`. Every entry requires a nonempty provenance `source` plus
exactly `text` for `available`, or a nonempty `reason` for `unavailable`.
Empty snapshot text is an explicit policy with no restrictions. Unknown fields,
invalid UTF-8/Unicode, duplicate JSON keys, duplicate normalized origins,
non-origin scopes, and unreadable manifests fail with exit 2 before any output
write. Output aliases to the manifest are protected just like request/policy
inputs. Snapshot URLs are never opened; `source` is only a label.

Use **`--max-robots-bytes N`** with `--robots`/`--user-agent` to set an optional
positive integer UTF-8 byte cap **per available snapshot**. The default has no
cap. The count includes the entire supplied text (BOM, comments and line endings),
not its JSON escape spelling or character count; exactly N bytes is accepted.
Every available entry is checked, including unused origins. Unavailable entries
have no text to cap; source/reason labels are not capped. Sizes are not summed.
For example, this fixture's available snapshot is 39 bytes:

```sh
# Expected exit 0 at the exact limit; exit 2 with --max-robots-bytes 38.
qawrench frontier examples/frontier/robots-requests.jsonl \
  --robots examples/frontier/robots-colonless-snapshots.json --user-agent AuditBot \
  --max-robots-bytes 39 --dry-run
```

An oversized snapshot fails input validation before rule parsing or any output
write, with canonical `origin`, original `source`, `actual_bytes` and
`limit_bytes` on stderr. Existing frontier, checkpoint **and report** files are
preserved (or remain absent); this input error does not produce a rejected audit
report. Accepted/rejected dry runs write nothing. The cap works in informational,
enforced, strict and checkpoint-only modes; it does not bypass their audit gates
or change accepted reports. It is not saved in checkpoints. Omit it to retain
existing behavior. The manifest is still read/decoded in full and held in memory:
this bounds individual snapshot parsing, not total input memory or snapshot
freshness.

Matching uses [RFC 9309 sections 2.2.1–2.2.3](https://www.rfc-editor.org/rfc/rfc9309.html#section-2.2.1):
case-insensitive exact product-token groups are combined, falling back to all
`*` groups only if no exact group exists. Paths match case-sensitively from
the beginning, with `*` and terminal `$`; the longest normalized rule path
(including pattern operators) wins, with allow on ties. Percent-encoded ASCII
unreserved bytes are decoded for comparison, escaped reserved bytes stay
escaped, and Unicode becomes UTF-8 percent octets. Literal `*`/`$` can be
written as `%2A`/`%24`. Query text participates; fragments do not. Empty rules
have no effect; no matching rule allows access, and `/robots.txt` is implicitly
allowed for an available policy. Invalid and non-rule lines are recorded in
`diagnostics`; parseable rules still apply. CR, LF, CRLF and an initial BOM
are supported. This is a local audit, with no HTTP status, redirect, cache-age,
network failure, crawl-delay or sitemap-fetch interpretation.

`report.robots` includes the selected token, decision counts, and each origin
encountered in valid input order. Policy scope normalizes scheme/host case,
Python IDNA 2003 hosts, IPv6 spelling and default ports; schemes, subdomains
and other ports remain separate. This does not change request identity.
Each available origin records its source, snapshot SHA-256, selected agent
lines and diagnostics. Each original valid request, including duplicates,
records its input index/location, original URL, comparison target, decision,
reason, every matching rule and the winning rule's directive/pattern/line.
Unavailable and absent snapshots retain `unknown` decisions with reasons.
Unused manifest origins are omitted. Snapshots are assertions supplied by the
caller; their freshness and correspondence to a live server are not verified.

For library use, construct `RobotsPolicy.from_dict(manifest)` (optionally with
`max_robots_bytes=N`; a positive integer, excluding booleans, or `None` for no
cap) and pass it as
`audit(requests, Policy(), robots=robots, user_agent="AuditBot")`. Add
`enforce_robots=True` for the same all-originals gate, and `strict_robots=True`
for the additional malformed-rule gate (requires `enforce_robots=True`). Both
default to `False`; other value types are rejected. The returned list is still
the simulated candidate on rejection, consistent with the library contract below; callers
must check `report["status"]` before using it. Enforcement uses the same matching
and diagnostic behavior as the informational audit. Only explicit strict mode
rejects the four malformed-rule categories independently of request
decisions. Neither mode validates snapshot freshness.

## Input and policy contract

Default format: UTF-8 JSONL, one object per line:

```json
{"url":"https://example.test/search?tag=b&tag=a&blank=","method":"POST","body":"a=1&a=2","source":"synthetic:seed"}
```

`url` is required. `method` defaults to `GET`; supplied methods must be uppercase
HTTP tokens, 1–32 characters. Lowercase methods fail instead of being silently
merged. `body` defaults to the empty string and must be a Unicode string;
null, binary and object bodies fail. Its whitespace, case, Unicode spelling,
and JSON/form field order are never normalized. Absent and empty bodies are
equivalent. Optional `source` is a provenance string and `metadata` is a JSON
object. These provenance fields are outside the request key; all originals
remain in witnesses when they differ.

Only HTTP(S) URLs with valid DNS/IDNA or bracketed IPv6 hosts and optional ports
1–65535 are supported. Unicode IRIs are accepted; malformed escapes, userinfo,
IPv6 zones, whitespace, controls, invalid hosts/ports and unescaped forbidden
characters are rejected. No DNS resolution occurs. This is a deliberately
narrow validator, not a browser's permissive URL repair algorithm.

`--format crawlee` accepts a local JSON array of URL strings or objects with
`url`, optional uppercase `method`, string `payload`, object `userData`, and
string `label`. `payload` becomes `body`; `userData`/`label` are retained under
`metadata`. Unsupported fields fail explicitly, including `headers`,
`uniqueKey`, `keepUrlFragment`, `useExtendedUniqueKey`, `requestsFromUrl`,
queue-state fields and future options. They are never ignored or fetched.
This is a tested **Crawlee JS 3.18 documented sources subset**, not an adapter
for every Crawlee request queue export or the Python Crawlee API.

A version-1 policy (all fields optional, defaults shown):

```json
{
  "version": 1,
  "remove_query_keys": [],
  "fragment": "preserve",
  "w3lib_sort_query": false,
  "idna": "preserve",
  "allow_ambiguous_merges": false
}
```

- The baseline compares `(method, original URL, exact body)`. It preserves
  query order, repeated pairs, blanks, bare flags, escape spelling, host case,
  port spelling, fragments and even empty `?`/`#` delimiters.
- `remove_query_keys` lists exact, case-sensitive **decoded ASCII** key names.
  Query fields are separated only by `&`; percent escapes and `+` are decoded
  for key comparison only. Every occurrence of a named key is removed; all
  remaining fields keep their original spelling and order. No wildcard or
  automatic `utm_*` removal. Key names containing whitespace or `&=%+#` fail.
- `fragment: "drop"` removes the `#` and everything after it. Applications can
  use fragments as meaningful client routes; dropping them can be unsafe.
- `w3lib_sort_query: true` invokes the real w3lib 2.3.1 query canonicalizer.
  It sorts by key **and value**, retains blank values/repeated-pair multiplicity,
  normalizes query encodings/spaces, and can rewrite bare flags/empty fields.
  It is not just a stable sort. Host, path, port and fragment are excluded
  from that call. Defaults preserve exact values and multiplicity.
- `idna: "ascii"` explicitly applies Python's IDNA 2003 codec to the host,
  including its mappings/case behavior. It is not UTS-46 or browser IDNA.
- **Any observed policy-created request merge fails by default.** Proposed URL
  collisions between different original URLs with distinct methods/bodies also
  fail, although their request fingerprints remain separate. Set
  `allow_ambiguous_merges: true` explicitly to allow these with warnings after
  inspecting the witnesses. This flag acknowledges uncertainty; it does not
  prove semantic equivalence. Normalization without an observed merge passes.

Unknown policy/request fields, duplicate JSON keys and nonfinite numbers fail.
Invalid JSONL lines and unsupported records appear in `issues`; default handling
rejects the run. `--on-invalid skip` explicitly permits a partial frontier and
preserves each excluded record and its error. Blank lines are invalid records.
Broken array JSON, invalid UTF-8, unreadable files and invalid policies are fatal.

## Output and failure behavior

`frontier.jsonl` contains the **first original URL**, exact body and method for
each proposed identity, in original input order. It does not rewrite the URL
that would be sent. Crawlee metadata is retained and the resulting frontier is
readable again as this tool's JSONL. The canonical URL used for comparison is
in the report, not substituted into the representative.

`report.json` is deterministic for the same input/policy and contains:

- `counts`: raw records, valid/invalid/unsupported, raw URL-string unique count,
  exact/conservative request-key unique count, proposed unique count, accepted
  representatives, duplicate occurrences and policy-created collision groups.
  These counts describe the simulated candidate even if the run is rejected.
- `exact_collision_groups` and `collision_groups`: **every** repeated exact or
  proposed key, all original members/locations, representative index, exact body
  and method, and whether distinct baseline keys were merged.
- `changed_key_witnesses`: every changed URL and its before/after fingerprints;
  `protected_url_groups`: shared canonical URLs whose methods/bodies remain
  separate; provenance differences, representative originals, errors/warnings.

Deduplication compares complete tuples; SHA-256 fingerprints are display IDs
only. No hash collision can merge requests. No timestamps or timing noise are
added to the audit report.

Exit **0**: audit accepted. Exit **2**: invalid input/policy, refused merge,
robots enforcement rejection, batch ID conflict or I/O failure. Rejected audits with parseable
policy/input write the report but leave the frontier untouched; use `status`
and `frontier_emitted` to avoid treating an
old frontier as current. File-level/policy failures print an error and emit no
new report. Individual writes use temporary files and atomic replacement;
the frontier/report pair is **not** a two-file transaction. If report writing
fails after frontier replacement, the command fails and the frontier may have
changed. Output parents must exist; input/policy/output aliases and symlink
outputs are rejected. Reports contain exact supplied bodies/provenance.
Optional checkpoint writes follow the additional ordering described above;
checkpoints also contain all input text and bodies.
`--dry-run` writes no files and prints the report to stdout; it never executes
body strings, remote sources or scripts.

## Reproduce the experiment and tests

From a qawrench checkout:

```sh
uv run --extra test pytest tests/frontier -q
uv run --extra frontier python examples/frontier/experiment.py
# Explicitly save a new measurement (default is stdout only):
uv run --extra frontier python examples/frontier/experiment.py --output /tmp/frontier-experiment.json
```

Tests run sequentially and cover normal/failure paths, actual w3lib behavior,
fixture CLI round trips, metadata retention, false-merge detection, dry runs,
refusal reports, alias protection and atomic write failures. Robots enforcement
tests cover disallow/unknown rejection, report retention, frontier/checkpoint
preservation, all-allow emission, historical collapsed originals, dry-run and
independent invalid-record/merge checks.
Colonless-core tests cover exact field boundaries, ASCII case/whitespace,
comments, colons inside values, extension lookalikes, scoped original witnesses,
tolerant reports, preserved outputs, dry runs and checkpoint history.
Snapshot-size tests cover exact UTF-8 limits (including multibyte text/BOM),
unused and unavailable origins, invalid options, preserved outputs, no-write
dry runs and existing gates under an accepted limit.
BOM-witness tests cover first-record core errors, all three record separators,
exact whitespace/comments, tolerant diagnostic compatibility, single/interior
BOM handling, extension boundaries, checkpoint-only preservation and dry runs.
Byte-range tests cover exact UTF-8 slicing, repeated records, per-origin and
whole-snapshot digest binding, mixed separators, multibyte text and final EOF.
Verifier tests cover retained strict witnesses, changed whole snapshots,
strict input types, missing/unavailable origins, source/digest/text/line
agreement, full-record span boundaries, repeated-record relocation rejection,
input immutability and no fetching, file I/O or checkpoint changes. Default
verification does not parse; opt-in reason tests cover every genuine category,
all wrong recognized reasons, valid/ignored records, whole-snapshot group state,
nonselected groups, BOM/multibyte data, mixed separators, repeated records, EOF,
option types and provenance-failure precedence.
Saved-report CLI tests cover explicit reason opt-in, exact omitted-flag output,
requested-scope labeling, mixed indexed semantic/provenance failures, flag grammar,
multiple origins, empty/malformed collections,
complete manifest/JSON validation, all indexed helper failures, changed bytes,
regular-file/size boundaries and unchanged input bytes/mtimes/directory inventory.
Collection tests cover omitted records that individually verify, explicit
canonical/unique available scope, full unused-manifest validation, complete and
zero-record collections, indexed invalid/unexpected/duplicate precedence, exact
missing bindings and deterministic scope/record ordering, BOM/multibyte/mixed
separators/repeated text/EOF, detached results and pure immutable operation.
Batch ID tests cover exact replay, conflicting text/format, CRLF preservation,
legacy migration, duplicate saved IDs, no-write rejection/dry-run, stable
historical witnesses and audit gates on replay.

[Recorded synthetic measurements](../examples/frontier/experiment-results.json) use seed
39, 100 entity groups, 2,300 records, and five timed repetitions per algorithm.
Each measurement includes key generation plus set construction on already
validated in-memory records; parsing, file I/O and report generation are
excluded. The same seeded shuffle and labels are used for all algorithms.
The script additionally executes the CLI end to end in a temporary directory,
asserts repeated-key/body distinctions and reports every mixed-label key group.

The fixture intentionally contains meaningful fragments, order-sensitive
repeated arguments, bare-versus-assigned empty flags, byte-valued escapes and
multiple methods/bodies. Labels represent invented application semantics.
See [measured results and interpretation](frontier/experiment.md): more deduplication
includes false merges, and policy processing costs additional CPU time. These
results establish **no real-site improvement** or general equivalence rule.

## Library

```python
from pathlib import Path
from qawrench.frontier import Policy, audit, load_requests

requests, issues, total = load_requests(Path("examples/frontier/requests.jsonl"))
frontier, report = audit(requests, Policy(), issues=issues, total=total)
assert report["status"] == "ok"
```

The library returns the simulated candidate even when `report['status']` is
`rejected`; callers must check status before using it. The CLI enforces this.

For checkpoint library use, `Checkpoint(Policy()).append(text, "jsonl")`
creates a history, `to_dict()` serializes it, and
`Checkpoint.from_dict(data)` validates a saved history. Call `append` for the
next batch, then pass `load_requests()`'s `(requests, issues, total)` to
`audit` with the checkpoint's `policy`. If requesting a different policy,
inspect `policy_conflicts(requested)` and refuse nonempty conflicts, as the
CLI does. History objects are immutable; `append` returns a new checkpoint.
Pass `append(text, "jsonl", batch_id="seed:a")` for stable batch identity.
Identical replay returns the same object. Conflicting reuse raises the exported
`BatchConflict` (an `AuditError`) with the CLI's witness in `.conflict`, leaving
the old object unchanged. Catch it and refuse emission. Loading saved state
validates IDs and uniqueness; no external filename or ID registry is consulted.

Limitations: inputs/reports are held in memory; no fetching, scheduler, live
robots verification, production data, security scanning, remote lists or binary payloads.
Headers/cookies and other request-affecting options are outside the supported
schema and rejected. Policies are global, not site-specific semantic proofs.
Full Crawlee queue compatibility, real-world performance/adoption and demand
remain **unverified**. No claim that query sorting is always equivalent or that this
eliminates all crawl traps.

## License

MIT; see [`../LICENSE`](../LICENSE). Third-party notices (w3lib, robots.txt and
Crawlee format references) are in [`../THIRD_PARTY.md`](../THIRD_PARTY.md).
