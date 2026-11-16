# qawrench crawlee-export

`qawrench crawlee-export` (formerly `crawlee-parquet-export`) converts a **local,
stopped Crawlee dataset** into typed Parquet, entirely offline. An explicit schema
validates every row. Output includes row provenance and a JSON manifest; the exporter
reads back the staged Parquet and checks the schema, row count and value digest
before publishing it. Invalid input commits zero rows.

Crawlee already provides dataset storage and JSON/CSV exports, and Arrow already
writes typed Parquet. This adapter adds strict ingestion of Crawlee's numbered
item files, numeric ordering, schema enforcement, bounded row batches, provenance,
and failure accounting. An opt-in declared union schema handles nullable fields
that appear or disappear across batches. It does not crawl, use Apify credentials,
upload, infer types or undeclared fields, or claim compression/performance gains.

## Install

Requires Python 3.11 or newer. This command needs [PyArrow](https://arrow.apache.org/docs/python/),
installed via the `parquet` extra (tested with PyArrow 23.0.1 on Linux x86_64; the
PyArrow wheel is about 45 MiB, no browser or model download is needed):

```sh
pipx install 'qawrench[parquet]'      # or: pip install 'qawrench[parquet]'
qawrench crawlee-export --version     # prints the bare version, e.g. 0.1.0

# Or run without installing:
uvx --from 'qawrench[parquet]' qawrench crawlee-export --help
```

`python -m qawrench crawlee-export` is equivalent to `qawrench crawlee-export`.
Without PyArrow installed, the command prints an install hint for the extra and exits.
Installation may access the package index. **The command itself never accesses the
network.** A platform without a compatible PyArrow wheel must use a supported
interpreter/platform; source-building Arrow is outside this tool.

The example inputs below live in `examples/crawlee_export/` in the qawrench
repository; run the commands from the repository root (or adjust the paths).

## Working examples

Everything in `examples/crawlee_export/` is **synthetic offline test data**, not a real crawl.
Choose an unused output path; existing output or sidecar is never overwritten.
These commands create `/tmp/cpe.parquet` and `/tmp/cpe.parquet.manifest.json`:

```sh
qawrench crawlee-export examples/crawlee_export/dataset --schema examples/crawlee_export/schema.json --out /tmp/cpe.parquet --batch-size 2
python examples/crawlee_export/verify_roundtrip.py /tmp/cpe.parquet
qawrench crawlee-export --verify /tmp/cpe.parquet
```

The independent example verifier checks the bundled dataset's three exact expected
rows, including `A, B\nC "quoted" — café 東京`, null versus empty string, the signed
int64 endpoints and `9007199254740993`. It is specific to that fixture; use
`--verify` for arbitrary exports. The fixture command produced 3 rows in 2 row
groups. Synthetic results do not establish behavior or improvements on real sites.
Spark execution and interoperability with specific Spark releases are not tested.

JSONL is a separate explicit input mode (one object per physical line):

```sh
qawrench crawlee-export examples/crawlee_export/items.jsonl --input-format jsonl --schema examples/crawlee_export/schema.json --out /tmp/cpe-jsonl.parquet --batch-size 2
qawrench crawlee-export --verify /tmp/cpe-jsonl.parquet
```

Each read-only mode flag may occur only once: `--verify`, `--inspect-resume`,
`--check-schema`, `--check-export-schema`, `--check-exports-schema`,
`--check-resume-schema`, `--check-export-resume-schema` and
`--check-resume-export-schema`. Repeated occurrences, with identical or different paths,
produce argparse stderr/exit **2** and **no stdout**, before artifact/source reads
or checkpoint locking. For example, `--verify a.parquet --verify b.parquet`
fails with `argument --verify: may only be specified once`. Different modes are
mutually exclusive. A single occurrence retains its documented JSON and exits.

The single-path modes require a nonempty operand: `--verify ""`, `--verify=`,
`--inspect-resume ""` and `--inspect-resume=` produce argparse stderr/exit **2**
and **no stdout**, with `argument FLAG: must not be empty`. This applies alone,
before or after complete export arguments, and with batch settings; an empty
mode never falls through to export. Rejection precedes dispatch, artifact/source
reads, writes and checkpoint locking. Validation happens after argument parsing,
so argparse help, malformed arguments, repeated flags and distinct-mode conflicts
retain their precedence. Nonempty paths are passed through without stripping.

Both paths in each comparison mode must also be nonempty: `--check-schema`,
`--check-export-schema`, `--check-exports-schema`, `--check-resume-schema`,
`--check-export-resume-schema` and `--check-resume-export-schema`
reject an empty first, second or both operands with
`argument FLAG: paths must not be empty`. For example,
`--check-export-schema previous.parquet ""` produces argparse stderr/exit **2**
and **no stdout**, before verifying the previous export. The same rule applies
with conflicting export/batch options, before schema/artifact/source reads,
inspection dispatch, writes or checkpoint locking. It runs after parsing with
the same help/malformed/duplicate/distinct-mode precedence described above.
Nonempty comparisons retain their documented JSON reports and exits.

Verification accepts `--verify PARQUET` and optional `--batch-size N` (positive
integer, default **1000**) to bound read-back row batches. It returns the original
manifest, including the export's recorded batch settings. Source, `--schema`,
`--out`, `--resume-dir`, `--input-format`, `--batch-bytes` and `--max-item-bytes`
are export-only options and cannot accompany `--verify`. Explicit export-only
settings are argument errors even when equal to their defaults or placed before
the mode: `--input-format dataset --verify a.parquet` produces argparse stderr,
exit **2**, and **no stdout**, before artifact/source reads or checkpoint locking.
Export path arguments conflict by presence, including explicitly empty strings:
`--verify a.parquet --schema ""`, `--out= --verify a.parquet`,
`--verify a.parquet --resume-dir ""` and `"" --verify a.parquet` all fail with
`--verify cannot be combined with source, --schema, --out or --resume-dir`.
This applies before or after the mode and with separated or equals option syntax;
omitting a path argument is different from supplying an empty one.
Plain verification and verification with `--batch-size` retain manifest JSON/exit
0 on success and JSON stderr/exit 2 for invalid artifacts or nonpositive batch
sizes. These restrictions do not change ordinary export options.

## Resuming interrupted exports

For either input mode, opt into durable checkpoints with `--resume-dir` (library:
`resume_dir=...`). Its parent must exist. Use a new or empty directory separate
from the source, schema and output; one exporter holds an exclusive POSIX lock.
Repeat the **same command** after an interrupt or process kill:

```sh
qawrench crawlee-export examples/crawlee_export/items.jsonl --input-format jsonl --schema examples/crawlee_export/schema.json --out /tmp/cpe-resumed.parquet --batch-size 2 --resume-dir /tmp/cpe-checkpoint
# If interrupted, repeat that exact command.
qawrench crawlee-export --verify /tmp/cpe-resumed.parquet
```

For the bundled three-row JSONL input, interruption after the first two-row
checkpoint leaves no published Parquet. Retrying reuses those two rows and adds
only row three. The result has exactly the clean export's values, order, provenance,
row groups and value digest, including Unicode, nulls and exact int64 values.
Uncheckpointed rows are replayed; no rows are skipped or duplicated. A late invalid
row still prevents publication and preserves earlier checkpoints for inspection.

`resume.json` version 2 binds the resolved source path, ordered filenames, byte
lengths and SHA-256 hashes (including ignored dataset metadata), normalized explicit
schema, destination, Arrow/tool versions and all batch limits. Changing any binding
fails with exit 2. To export modified inputs, use a new checkpoint and output.
The source must remain stopped: full streaming hashes are checked at startup and
again before publication, but this does not make live writers safe.

Every part records `next_jsonl_offset`: the exact byte position immediately after
its last completed physical line, including LF/CRLF bytes (EOF for an unterminated
final line). For example, bytes `{"n":1}\r\n{"n":2}\n{"n":3}` with a two-row
checkpoint record offset **17**. Retry seeks to byte 17 and emits physical line 3;
reading ahead to decide a batch boundary never advances the saved position.
Dataset parts record null instead. Offsets must be strictly increasing integers
(booleans are rejected), within the source, and end at exactly the cumulative
checkpoint row count. The full-source hash pass validates these boundaries using
bounded byte chunks; it does not decode or read completed lines individually.
A completed JSONL checkpoint must end at EOF. Missing/invalid offsets, a position
inside UTF-8 or CRLF, or a valid boundary after the wrong row fail with exit 2 and
no new publication. Version-1 checkpoints are refused explicitly; preserve them
for manual examination and use a new checkpoint directory and unused output destination.

Each checkpoint is one closed Parquet batch, read back and fsynced before its
size/count/schema/digests are recorded by atomic manifest replacement and directory
fsync. Resume verifies every referenced part before reuse. A kill before a manifest
update leaves an unreferenced generated part that is discarded and replayed.
Assembly is restarted from complete parts; incomplete Parquet files are never
appended to. If a kill occurs during publication, retry accepts only artifacts
byte-identical to the reconstructed export, completing an orphan sidecar safely.
Unrelated existing artifacts and symlinks are rejected. A fully published retry is
also verified and succeeds without overwriting it. Ordinary exports without
`--resume-dir` retain their existing no-clobber behavior.

Checkpoints remain after success: remove that specific directory yourself after
successful `--verify`. Keep it private and do not edit its manifest or parts. This
is accidental-corruption detection, not authentication against malicious edits.
Checkpointing uses additional disk space for parts plus assembled output, extra
writes/read-back, and full-source hashing on each invocation. JSONL row ingestion
seeks past completed physical lines; dataset resume skips completed items.
The complete source is still read twice for hash/boundary checks on successful
invocations. Filename and checkpoint indexes use O(items + batches) memory, and the
manifest is rewritten per batch. No speed or fixed-RSS improvement is claimed.
Abrupt kills can still leave `.cpe-*.tmp` assembly files in the output parent;
resume does not delete unknown files there. A damaged/missing manifest with existing
parts is refused; preserve it for inspection and start a new job.

### Inspecting a checkpoint before retry

Use an existing checkpoint directory, with the source stopped and no exporter
running. No source/schema/output arguments or batch overrides are accepted:

```sh
qawrench crawlee-export --inspect-resume /tmp/cpe-checkpoint
```

The library equivalent is `inspect_resume(directory)`, exported from
`qawrench.crawlee_export`. Inspection uses the stored normalized schema, source path,
destination and settings; the original schema file is not needed. It supports
checkpoint format **2** and the recorded tool/PyArrow runtime versions only.
For the two-row interrupted JSONL example above, the JSON stdout report has this
shape (the actual offset depends on the source bytes):

```json
{"resume_version":2,"source":{"path":"/absolute/items.jsonl","input_format":"jsonl"},"part_count":1,"reusable_rows":2,"next_jsonl_offset":17,"source_complete":false}
```

`reusable_rows` counts only committed, verified parts. `next_jsonl_offset` is the
last saved byte offset, **0** when no JSONL parts exist, or **null** for dataset
input. `source_complete` is true only when `source_row_count` is set and verified
against EOF (JSONL) or the dataset item count. A completed empty checkpoint reports
zero parts/rows, offset 0 (JSONL) or null (dataset), and true. An initial checkpoint
saved before any rows reports false. Completion describes checkpointed ingestion;
it does **not** assert that destination publication occurred or that an existing
destination is valid. Use `--verify` separately to check a published export.

Exit **0** means the complete source fingerprint matches its binding, all referenced
parts pass size/hash/schema/row-count/value-digest/row-group checks, every JSONL
boundary/count is valid, and a final full-source recheck passed. Changed source,
missing/corrupt/symlinked referenced files, malformed settings/offsets/completion,
unsupported versions, and an active exporter lock produce JSON stderr and exit
**2**, with no success report on stdout. Conflicting CLI modes/options use argparse
stderr and exit 2. Invalid unprocessed source rows are still validated on retry;
inspection does not parse or certify the remaining rows against the schema.

Inspection takes a shared nonblocking lock on the existing checkpoint directory.
It creates no directory, lock file, temporary, checkpoint or output, performs no
cleanup, and never opens the bound destination/sidecar. Manifest, referenced parts,
unreferenced entries (including generated parts/temporaries), source and destination
bytes/mtimes remain unchanged; reading may update access times. Unreferenced files
are ignored, never adopted or certified. This is read-only accidental-corruption
validation, not authentication of an edited checkpoint. Keep input quiescent;
checkpoint locks do not stop external source writers. Full hashing and Parquet
read-back are required; no performance improvement is claimed.

Export and verification print the manifest as JSON to stdout. Export errors print
JSON on stderr, with exit code 2, the failing filename/line where applicable,
`rows_validated` before the failure and `rows_committed: 0`. Interrupts exit 130.
Argument errors use argparse's diagnostics. No bad rows are skipped. No scripts
are executed and no remote/production resources are accessed; there is no dry-run
mode because normal operation only creates a new explicitly named local output.

Library use:

```python
from qawrench.crawlee_export import export_dataset, verify_export, ExportError

manifest = export_dataset(
    "examples/crawlee_export/dataset", schema="examples/crawlee_export/schema.json",
    out="/tmp/library-cpe.parquet", batch_size=2,
)
assert manifest["row_count"] == 3
verify_export("/tmp/library-cpe.parquet")
```

## Supported input contract

This is the JavaScript Crawlee local memory-storage dataset layout, inspected at
**v3.15.3**, not the Apify cloud API, Python Crawlee storage, CSV, or an array-style
JSON export. See the pinned [dataset client source](https://github.com/apify/crawlee/blob/v3.15.3/packages/memory-storage/src/resource-clients/dataset.ts)
and [filesystem entry source](https://github.com/apify/crawlee/blob/v3.15.3/packages/memory-storage/src/fs/dataset/fs.ts).
No Crawlee runtime was executed: source-shaped synthetic fixtures test the format.
Other upstream versions are not independently validated.

* Dataset mode accepts a flat directory of `[0-9]{1,128}.json` regular files,
  each containing exactly one UTF-8 JSON object. Files are sorted by the integer
  filename, not lexically. Nonnegative IDs, gaps and non-padded names are allowed;
  `1.json` plus `000000001.json` is a duplicate numeric ID and fails. Business
  fields named `id` have no uniqueness constraint.
* The pinned source starts at `000000001.json` and pads to at least nine digits.
  The general [result-storage guide](https://crawlee.dev/js/docs/guides/result-storage)
  describes zero-based indices; accepting nonnegative IDs accommodates that
  discrepancy without silently shifting or renumbering rows.
* **Only `__metadata__.json` is ignored**, per the pinned
  [metadata writer](https://github.com/apify/crawlee/blob/v3.15.3/packages/memory-storage/src/background-handler/fs-utils.ts).
  Its content is not trusted for row counts. All other files, hidden files,
  directories and symlink entries fail. Put your schema outside the dataset.
* JSONL mode requires `--input-format jsonl`. It preserves physical line order,
  accepts LF/CRLF and an unterminated final line. Blank lines fail; literal
  newlines within strings must be JSON escaped. An empty input is a valid typed
  zero-row export.
* Malformed JSON, duplicate keys at any nesting level, non-UTF-8 text, unpaired
  Unicode surrogates, NaN/infinity and floating overflow fail. JSON must use the
  standard finite numeric syntax. Inputs must be a **quiescent snapshot**; live
  crawling or concurrent source modification is unsupported.

## Explicit schema

This is the adapter's schema format, not JSON Schema or Arrow's inference format:

```json
{
  "version": 1,
  "fields": [
    {"name": "title", "type": "string"},
    {"name": "price", "type": "float64"},
    {"name": "count", "type": "int64"},
    {"name": "active", "type": "bool"},
    {"name": "note", "type": "string", "nullable": true},
    {"name": "tags", "type": "json-string"}
  ]
}
```

In version 1, all fields must be **present**, including nullable fields. `nullable`
defaults to false and permits explicit JSON null only. Missing and extra fields
fail. Version 2 adds the explicit evolution policies below. There is no
discard-extra or infer-schema option. Field names must be unique,
nonempty strings; the `_cpe_` prefix is reserved. Unknown schema keys/types and
versions fail. Schema input is limited to 1 MiB.

| Declared type | Accepted JSON / Parquet representation |
| --- | --- |
| `string` | JSON string → Arrow UTF-8 string. No stripping, newline or Unicode normalization. |
| `int64` | Integer token, signed −2^63 through 2^63−1 → Arrow int64. Never routed through a float. |
| `float64` | Finite decimal/exponent token, e.g. `12.5` or `1e2` → Arrow double. `12` is rejected: write `12.0`. |
| `bool` | JSON true/false → Arrow boolean. Integers and strings rejected. |
| `json-string` | Explicitly opted-in JSON value → Arrow UTF-8 string containing canonical JSON. Object/array structure is retained within the string. |

There is no string-to-number, bool-to-int, integer-to-double, or nested-to-string
coercion outside `json-string`. Float tokens use Python/IEEE-754 binary64 parsing
(the usual decimal rounding and underflow semantics, not arbitrary precision
decimals). Use `int64` for exact integers. `json-string` canonicalization uses
Python JSON semantics: sorted object keys, compact separators, literal UTF-8,
finite numbers; it is **not RFC 8785/JCS**. JSON null maps to SQL null when nullable;
a nested string `"null"` remains the canonical string value, not SQL null.
`json-string` fields retain their logical type in Arrow field metadata.

Three additional columns always record row provenance:

| Column | Dataset mode | JSONL mode |
| --- | --- | --- |
| `_cpe_source` (non-null string) | Original item basename | Input basename |
| `_cpe_item_id` (nullable string) | Numeric filename normalized as decimal text | null |
| `_cpe_line` (nullable int64) | null | Physical line number starting at 1 |

## Nullable schema evolution

Declare the complete union of fields before exporting, including optional fields
introduced by later source batches. Use schema **version 2** and a required
`evolution` policy; the field declarations and type rules stay the same:

```json
{
  "version": 2,
  "evolution": "nullable-union",
  "fields": [
    {"name": "id", "type": "int64"},
    {"name": "note", "type": "string", "nullable": true},
    {"name": "score", "type": "int64", "nullable": true}
  ]
}
```

| Policy | Missing nullable field | Declared nullable field first appearing later | Missing required / undeclared added field / wrong type |
| --- | --- | --- | --- |
| `strict` | Error | Earlier missing rows fail | Error |
| `nullable-union` | Fill with SQL null | Accept, retaining typed nulls for earlier rows | Error |

The union policy also permits a nullable field to disappear again. A declared
field never seen in the source remains a typed all-null column. Presence rules
apply per source row, so changing row or byte batch limits does not change values
or field provenance. There is no discovery of new field types, schema mutation,
type promotion, or appending to existing output. To add an undeclared field, declare
it as nullable in the union schema and export to a new destination. Required fields
must appear in every row. Version 1 retains its existing strict output format.

Both version-2 policies add `_cpe_missing_fields`, a non-null Arrow list of non-null
UTF-8 field names in schema order. A name in this list means the field was absent
in that source row; its output value must be null. A null value with no corresponding
name means **explicit JSON null**. In strict mode the list is always empty. The
manifest's `field_provenance` reports each declared field's `present`, `missing`,
`explicit_null`, and `first_present_row` (1-based output position, or null if never
present). `present` includes explicit nulls; `present + missing = row_count`.
Combine that row position with the source columns to locate the first occurrence.
The policy is embedded in Arrow schema metadata and bound into resume checkpoints.
Summary counts are rebuilt from reused parts, so interruption preserves them.

For input `{"id":1}`, `{"id":2,"note":null}`, `{"id":3,"note":"new"}`
with one row per batch, `note` reads back as `[null, null, "new"]`, while its
missing markers are `[true, false, false]`. Its summary is
`{"present":2,"missing":1,"explicit_null":1,"first_present_row":2}`.
An undeclared `{"added":null}` in a later row still fails with exit 2 and no output;
earlier staged row groups are not published.

The bundled five-row synthetic fixture additionally exercises disappearance,
empty string, Unicode/newline, and a never-present nullable int64 field:

```sh
qawrench crawlee-export examples/crawlee_export/evolution/dataset --schema examples/crawlee_export/evolution/schema.json --out /tmp/cpe-evolution.parquet --batch-size 1
python examples/crawlee_export/verify_evolution.py /tmp/cpe-evolution.parquet dataset
qawrench crawlee-export --verify /tmp/cpe-evolution.parquet
qawrench crawlee-export examples/crawlee_export/evolution/items.jsonl --input-format jsonl --schema examples/crawlee_export/evolution/schema.json --out /tmp/cpe-evolution-jsonl.parquet --batch-size 1
python examples/crawlee_export/verify_evolution.py /tmp/cpe-evolution-jsonl.parquet jsonl
```

The standalone verifier imports only PyArrow and the standard library. It asserts
literal expected rows, Arrow types/nullability, presence markers, policy metadata,
per-field counts and file/value digests. Export readback and `--verify` independently
read physical Parquet rows, validate missing markers and recompute the summary
against the sidecar. Summaries use O(fields) memory; the existing 1 MiB sidecar
limit still applies, so very large schemas can fail before publication.

## Schema compatibility preflight

Before re-exporting with a changed declaration, compare the **old schema to the
new schema**, in that order. This read-only command opens only those two schema
files; it does not read source rows, Parquet or checkpoints, load PyArrow, or
create output. Both files use the explicit format above and the same 1 MiB limit.

```sh
qawrench crawlee-export --check-schema examples/crawlee_export/evolution/schema-before.json examples/crawlee_export/evolution/schema.json && \
  qawrench crawlee-export examples/crawlee_export/evolution/dataset --schema examples/crawlee_export/evolution/schema.json --out /tmp/cpe-preflight.parquet --batch-size 1
```

The first command emits exactly this JSON object (key order is not significant):

```json
{"compatible":true,"added_nullable_fields":["note","score"],"relaxed_nullable_fields":[],"issues":[]}
```

Exit **0** means compatible. Exit **2** with `compatible:false` reports every
incompatibility as JSON on **stdout**, with empty stderr. Invalid/unreadable
schemas instead return the usual JSON error on stderr, no stdout, and exit 2.
Argument errors use argparse diagnostics and exit 2. The comparison mode cannot
be combined with export/verify options, including explicitly supplied batch limits.
Each comparison flag (`--check-schema`, `--check-export-schema`,
`--check-exports-schema`, `--check-resume-schema`,
`--check-export-resume-schema`, `--check-resume-export-schema`) may occur only once per
invocation. Repeating the same flag, even with identical paths, produces argparse
stderr and exit 2 with no stdout, before any schema, export or checkpoint is read.
For example, `--check-schema old.json new.json --check-schema old.json new.json`
fails with `argument --check-schema: may only be specified once`. Different modes
remain mutually exclusive; a single occurrence keeps the report/exit rules above.
The library equivalent is
`check_schema_compatibility(old_schema_path, new_schema_path)`, imported from
`qawrench.crawlee_export`; it returns the same report or raises `ExportError` for
invalid input.

Comparison matches exact field names, independently of declaration order:

| Change | Result / issue code |
| --- | --- |
| Identical fields or reordered declarations | Compatible |
| Existing required field becomes nullable | Compatible; named in `relaxed_nullable_fields` |
| New nullable field, target policy `nullable-union` | Compatible; named in `added_nullable_fields` |
| Removed/renamed field | `field_removed` (a rename is removal plus addition) |
| Any declared type change, including `string` ↔ `json-string` | `type_changed`; no promotion/coercion |
| Existing nullable field becomes required | `nullability_tightened` |
| New required field | `required_field_added` |
| New nullable field under a strict target | `nullable_addition_requires_union`: old rows would still fail for omitting it |
| `nullable-union` becomes `strict` | `evolution_tightened`, conservatively refused even without current nullable fields |

Version-1 strict and version-2 strict are equivalent for this comparison; strict
may become nullable-union. Issues follow old field order (removal, type change,
then nullability), new field order for invalid additions, then policy tightening.
Each issue has `code` and `field`, except policy tightening; type, nullability and
policy changes also carry `old` and `new`. For example, changing a nullable int64
`amount` to required float64 returns:

```json
{"compatible":false,"added_nullable_fields":[],"relaxed_nullable_fields":[],"issues":[{"code":"type_changed","field":"amount","old":"int64","new":"float64"},{"code":"nullability_tightened","field":"amount","old":true,"new":false}]}
```

Added names follow new schema order; relaxed names follow old schema order.
The report checks declared row compatibility, not the actual data or downstream
readers. Physical field order and version-specific provenance columns can change.
A compatible result still requires a complete, normally validated re-export to
an **unused destination**; existing output and checkpoints keep their schema
bindings. The command neither migrates existing files nor appends to them.

### Using a verified previous export as the baseline

When the previous export is available, use its verified schema directly:

```sh
qawrench crawlee-export --check-export-schema /tmp/cpe-previous.parquet examples/crawlee_export/evolution/schema.json
```

`PREVIOUS.parquet` and `PREVIOUS.parquet.manifest.json` must both be regular,
non-symlink files. The sidecar must have supported manifest format 1 and schema
version 1 or 2. The command runs the existing `verify_export` first: file size and
SHA-256, physical Arrow schema, row count, value digest, row groups, and version-2
field provenance must all match. It then compares the verified manifest's schema
to the new bounded declared-schema file with exactly the rules and report above.
The original source and old schema file are unnecessary. Unlike `--check-schema`,
this mode requires PyArrow and reads the whole previous Parquet.

For an old strict export containing only required `id: int64`, the bundled union
target produces the same `note`/`score` nullable-addition report shown above.
Compatible reports exit **0**; incompatible reports use stdout/exit **2** and empty
stderr. Missing, corrupt, mismatched, unsupported or symlinked artifacts, or an
invalid new schema, instead produce JSON error stderr/exit **2** with **no stdout**.
A valid-looking sidecar alone is insufficient. New schemas use the same strict
parser, regular-file requirement and 1 MiB bound as `--check-schema`.

Only the two paths are accepted: export arguments, explicit batch limits, and
`--verify`, `--check-schema`, `--check-exports-schema`, `--check-resume-schema`,
`--check-export-resume-schema`, `--check-resume-export-schema` or `--inspect-resume` conflict
(argparse stderr/exit 2).
Preflight preserves previous Parquet, sidecar and schema bytes/mtimes and creates
no extracted schema, output, temporary or checkpoint. Reads may change access
times. Keep artifacts quiescent; the verification checks detect accidental
corruption, not malicious changes to both artifacts, and do not authenticate
origin or validate a future source dataset. Re-export still requires an unused
destination after a successful preflight; this command never writes one.

The library equivalent, exported from `qawrench.crawlee_export`, is
`check_export_schema_compatibility(previous_export_path, new_schema_path)`.

### Comparing two verified exports

When both exports are already available, compare their declared schemas directly:

```sh
qawrench crawlee-export --check-exports-schema /tmp/cpe-previous.parquet /tmp/cpe-candidate.parquet
```

The direction is **previous schema → candidate schema**. Both Parquet/sidecar
pairs must pass the full existing `verify_export` checks described above before
any comparison report is produced. The comparison consumes only those verified
schemas; neither original source nor original schema files are required. This
requires PyArrow and reads both exports, including their physical schemas,
row/value digests, row groups and version-2 field provenance. A sidecar alone
cannot establish either schema.

The rules, report and issue order match `--check-schema` and
`--check-export-schema`. Compatible reports use JSON stdout/exit **0**;
incompatible reports use JSON stdout/exit **2**, both with empty stderr. Invalid,
missing, corrupt, mismatched, unsupported, nonregular or symlinked artifacts on
either side produce JSON error stderr/exit **2** and **no stdout**. Comparing an
export to itself is allowed. Exactly two paths are accepted; every other mode,
export argument and explicit batch limit conflicts (argparse stderr/exit 2).

No output, temporary, checkpoint or lock file is created. Artifact bytes, mtimes
and directory listings are preserved; reads may change access times. Keep both
pairs quiescent. Existing verification detects accidental corruption, not
coordinated malicious edits, and does not authenticate origin. Compatibility
compares declarations; it does not establish equal rows, validate a future source,
certify downstream readers, migrate data or allow appending. A changed schema
still requires a new checkpoint and unused destination for a resumable re-export.

The library equivalent is
`check_exports_schema_compatibility(previous_export_path, candidate_export_path)`,
exported from `qawrench.crawlee_export`; it returns the report or raises
`ExportError` on invalid artifacts.

### Using a validated checkpoint as the baseline

If an export has not been published, preflight the schema bound to its checkpoint:

```sh
qawrench crawlee-export --check-resume-schema /tmp/cpe-checkpoint examples/crawlee_export/evolution/schema.json
```

This runs the full read-only checkpoint inspection described above, using its
stored parsed schema as the baseline. It requires a supported format-2 checkpoint,
matching tool/PyArrow versions, a quiescent bound source and valid referenced
parts. Source hashes, part size/hash/schema/row/value/group checks and JSONL
offsets/completion are validated, then the source is rechecked. It takes the same
shared nonblocking directory lock as `--inspect-resume`; an active exporter is
refused. It never trusts `resume.json` alone or rereads it to extract the schema
after verification. The original schema file and published Parquet/sidecar are
unnecessary; interrupted, zero-part, empty and completed checkpoints work for
both dataset and JSONL input.

The new schema uses the same bounded declared format, comparison rules and JSON
report as `--check-schema` and `--check-export-schema`. Compatible reports use
stdout/exit **0**; incompatible reports use stdout/exit **2**, both with empty
stderr. Invalid/missing/corrupt/symlinked checkpoints, bound sources or referenced
parts, invalid target schemas and locked checkpoints produce JSON error stderr,
exit **2** and no stdout. Exactly two paths are accepted; other modes, export
arguments and explicit batch options conflict (argparse stderr/exit 2).

No output, checkpoint, temporary, lock file or cleanup is written. Input bytes,
mtimes and directory listings, including unreferenced files, are preserved; reads
may change access times. Inspection does not certify remaining rows or destination
publication and does not authenticate artifacts. Keep source and checkpoint
quiescent. **A compatible result does not allow changing the existing resume
binding.** A changed schema still requires a new checkpoint directory and an
unused output destination.

The library equivalent is
`check_resume_schema_compatibility(checkpoint_directory, new_schema_path)`,
exported from `qawrench.crawlee_export`; invalid inputs raise `ExportError`.
It returns the same report and raises `ExportError` on invalid artifacts/schema.

### Comparing a verified export to a validated checkpoint

To compare a previous published export with a job whose destination may still be
unpublished, use **previous export schema → checkpoint bound schema**:

```sh
qawrench crawlee-export --check-export-resume-schema /tmp/cpe-previous.parquet /tmp/cpe-checkpoint
```

The command fully verifies the previous Parquet/sidecar pair, then fully inspects
the checkpoint with the shared nonblocking directory lock described above. It
uses only the schema returned by export verification and the exact parsed schema
returned by that locked inspection; it never extracts schema files or rereads a
sidecar or checkpoint manifest to obtain a schema after validation. Both sides
must pass before any comparison report is emitted. This requires PyArrow, a
supported export pair and a supported format-2 checkpoint, matching checkpoint
tool/PyArrow versions, a quiescent bound source and valid referenced parts and
JSONL offsets. Interrupted, zero-part, empty and completed dataset/JSONL
checkpoints work. Original schema files, the previous export's source and the
checkpoint destination Parquet/sidecar are unnecessary.

The report and deterministic issue order match the schema preflight rules above.
For example, a previous required `id: int64` compared with a checkpoint that makes
`id` nullable and adds nullable `extra` under `nullable-union` returns:

```json
{"compatible":true,"added_nullable_fields":["extra"],"relaxed_nullable_fields":["id"],"issues":[]}
```

Compatible reports use JSON stdout/exit **0**; incompatible reports use JSON
stdout/exit **2**, with empty stderr. Invalid, missing, corrupt, unsupported,
nonregular or symlinked export/sidecar/checkpoint/source/referenced parts, invalid
bindings or offsets, and an active exclusive checkpoint lock produce JSON error
stderr/exit **2** with **no stdout**. Exactly two nonempty paths and one occurrence
of the mode are required. Every other mode, source/export argument and explicit
input-format or batch setting conflicts (argparse stderr/exit 2). Existing help,
malformed-argument, duplicate and conflicting-mode parser precedence is retained.

No output, extracted schema, temporary or lock file is created; no cleanup,
publication or binding change occurs. Bytes, mtimes and listings, including
unreferenced checkpoint files, are preserved; reads may update access times.
Keep both export artifacts and checkpoint/source quiescent. Existing validation
detects accidental corruption, does not authenticate artifacts, and does not
certify remaining source rows, destination publication or downstream readers.
**Compatibility does not permit changing the existing resume binding**; a changed
schema requires a new checkpoint and unused output destination.

The library equivalent is
`check_export_resume_schema_compatibility(previous_export_path, checkpoint_directory)`,
exported from `qawrench.crawlee_export`; invalid input raises `ExportError`.

### Checkpoint baseline → candidate export

To use an interrupted job as the baseline for a published candidate, compare
**checkpoint bound schema → candidate export schema**:

```sh
qawrench crawlee-export --check-resume-export-schema /tmp/cpe-checkpoint /tmp/cpe-candidate.parquet
```

This fully inspects the baseline checkpoint under the existing shared nonblocking
directory lock, then fully verifies the candidate Parquet/sidecar pair. It compares
the exact parsed schema returned by locked inspection with the schema returned by
export verification. It does not reverse the export → checkpoint report: allowing
a nullable addition or nullability relaxation in one direction does not permit a
removal or tightening in the other. Both validations must pass before a report.

The report, issue order and exit behavior follow the rules above: compatible JSON
stdout/exit **0**; incompatible JSON stdout/exit **2**; empty stderr in both cases.
Invalid, missing, corrupt, unsupported, nonregular or symlinked checkpoint,
bound source, referenced parts or candidate artifacts, invalid bindings/offsets,
and active exclusive checkpoint locks yield JSON error stderr/exit **2** with
**no stdout**. Exactly two nonempty operands and one occurrence are required;
all other modes and explicit source/export/input/batch settings conflict before
artifact I/O. Existing argparse help and error precedence is preserved.

Interrupted, zero-part, empty and completed dataset/JSONL checkpoints are
supported under the same format-2 and tool/PyArrow version requirements as
inspection. Original schema files, the candidate's original source and baseline
destination Parquet/sidecar are unnecessary. The bound baseline source and both
candidate artifacts must remain quiescent. No extraction, writes, temporary or
lock files, cleanup, publication or binding changes occur. Bytes, mtimes and
listings are preserved; reads may update access times. Validation covers referenced
progress and candidate integrity, not unprocessed source rows, baseline destination
publication, artifact authenticity or downstream reader compatibility. A compatible
report does not authorize resuming with a changed schema or binding.

The library equivalent is
`check_resume_export_schema_compatibility(checkpoint_directory, candidate_export_path)`,
exported from `qawrench.crawlee_export`; invalid input raises `ExportError`.

## Batches, verification and publication

`ParquetWriter` writes uncompressed Parquet 2.6 with dictionaries disabled.
Defaults: `--batch-size 1000`, `--batch-bytes 16777216`,
`--max-item-bytes 8388608`. A batch flushes before exceeding its row or canonical
UTF-8 payload byte budget. A single canonical row larger than the batch budget
fails. Dataset file reads and JSONL line reads are bounded before JSON parsing.

These budgets bound row payload buffering, **not process RSS**: Python objects,
JSON parsing and Arrow allocations add overhead. The sorted filename/ID index
uses O(number of items) memory; Parquet footer metadata grows with row groups.
The tool does not load the entire dataset's values into memory. Read-back uses
single-threaded record batches within one row group at a time. Tests run serially.

The manifest contains the explicit schema, provenance columns, row count, row
group count, ignored metadata, batch limits, versions, Parquet byte length and
SHA-256, plus a value SHA-256. The value digest hashes each validated output row
(including provenance) as compact sorted-key UTF-8 JSON followed by LF. The
separate read-back path computes that digest from actual Parquet rows and checks
the exact Arrow schema and footer count. `--verify` repeats file and value checks
against the sidecar; this detects accidental corruption, not malicious changes to
both artifacts. It does not re-open original inputs or authenticate their origin.

Temporary files are created in the destination's existing parent directory. After
validation/read-back, both files are fsynced. Atomic no-clobber hard links publish
the manifest first and Parquet last; the directory is then fsynced. This requires
a local POSIX filesystem supporting hard links. Output permissions are 0600.
Existing outputs/sidecars, symlink destinations, input/schema collisions, and
output inside the dataset (including through parent symlink aliases) fail.
Ordinary failures remove staged files and any newly published sidecar.

A Parquet file becomes visible **complete**, never partially written. Two files
cannot be published as a single filesystem transaction: a reader can briefly see
only the manifest, and an abrupt process kill/power loss can leave temporary files
or an orphan sidecar. Require both files and successful `--verify` before consuming
an export. Without checkpoints, inspect/remove only known orphan files or choose a
new output path after such a crash. With `--resume-dir`, repeat the original command
as described above. Concurrent modification of output files/directories is unsupported.

## Tests

From a qawrench checkout:

```sh
uv run --extra test pytest tests/crawlee_export -q
```

The suite exercises actual writes/reads, multiple and byte-limited batches, numeric
ordering, int64 endpoints, signed zero, JSONL, strict schemas, malformed/ambiguous
JSON, late failures, missing dependencies, IO failures, no-clobber publication,
tampered-manifest/value verification, checkpoint reuse and binding rejection,
JSONL byte offsets with direct-seek assertions, wrong-row/broken boundaries,
mixed LF/CRLF, unterminated EOF, multibyte text and multi-chunk hash reads,
process-death recovery across checkpoint and publication boundaries, and nullable
evolution across row/byte batches with per-field provenance, strict failures,
resume equality and an independent PyArrow fixture verifier. No network, crawling
or browser is used. Schema preflight tests additionally cover directional
compatibility, all logical type pairs, combined diagnostics, policy tightening,
malformed/oversized/symlinked schemas, mode conflicts and read-only behavior
without Arrow. Verified-export, two-export and checkpoint preflight tests compare
reports across strict/union baselines, reject file/sidecar/schema corruption and
symlinks, and assert unchanged bytes/mtimes/listings without original source/schema
or write helpers. Read-only CLI mode tests reject repeated flags with
identical/different paths and different option positions before artifact I/O.

[Crawlee issue #1768](https://github.com/apify/crawlee/issues/1768) motivates the
quoting/newline and downstream analytics use case; it is not proof of a current
upstream defect. Adoption, demand and real-world gains are unverified. No
optimization experiment or compression claim is made.

## License

MIT; see [`../LICENSE`](../LICENSE). Third-party notices (PyArrow, Crawlee format
references) are in [`../THIRD_PARTY.md`](../THIRD_PARTY.md).
