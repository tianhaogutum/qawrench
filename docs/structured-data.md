# qawrench structured-data

`qawrench structured-data` (formerly `structured-data-parity`) is an offline comparison
of JSON-LD and OpenGraph in **two saved UTF-8 HTML files**. It uses real
`extruct.extract`, then pairs JSON-LD entities and reports missing entities/properties
and changed values with JSON pointers and source provenance. Python 3.11+; tested on
**CPython 3.14.4, Linux x86_64, extruct 0.18.0**.

Unlike [extruct](https://github.com/scrapinghub/extruct), which extracts several
metadata formats, this command adds a strict two-capture comparison, identity
validation, per-field string/array policy, saved comparison baselines, selector
coverage diagnostics, and JSON/Markdown reports. Extraction
remains extruct's work. This is a bounded structural comparison, not full JSON-LD semantic
equivalence, rich-result eligibility certification, or an SEO ranking tool.

## Install

Requires Python 3.11 or newer. extruct (and lxml) are optional dependencies of
`qawrench`, installed via the `structured` extra:

```sh
pipx install 'qawrench[structured]'    # or: pip install 'qawrench[structured]'
qawrench structured-data --version     # prints the bare qawrench version

# Or run without installing:
uvx --from 'qawrench[structured]' qawrench structured-data --help
```

`python -m qawrench structured-data` is equivalent to `qawrench structured-data`.
`pipx install 'qawrench[all]'` installs the optional dependencies of every command.
Without the extra, the command prints an install hint and exits 2. The tested
extruct version is 0.18.0 (reports record `"extractor": "extruct==0.18.0"`).
Dependency installation needs a package index; **running the command and its tests
needs no network**. No browser, model, or system packages are required.

## Run the synthetic example

All HTML files in `examples/structured_data/` are **synthetic offline fixtures**, not raw/rendered
captures from a real website. No browser rendering happens here.

```sh
qawrench structured-data examples/structured_data/raw.html examples/structured_data/rendered.html \
  --base-url https://fixture.test/ --out /tmp/delta.json

qawrench structured-data examples/structured_data/raw.html examples/structured_data/rendered.html \
  --base-url https://fixture.test/ --format markdown --out /tmp/delta.md
```

The JSON report contains exactly two changes for this pair (provenance omitted
below):

```json
[
  {"syntax":"json-ld", "entity":"https://fixture.test/fixture/product",
   "path":"/offers/price", "kind":"changed", "old":"12.00", "new":"14.00"},
  {"syntax":"opengraph", "entity":null, "path":"/og:title/0",
   "kind":"changed", "old":"Fixture mug", "new":"Fixture mug — rendered"}
]
```

Output defaults to JSON on stdout; `--out` atomically replaces the chosen report.
An output path resolving to an input is rejected. Valid comparisons exit **0**,
including those with changes. Add `--fail-on-diff` to exit **1** for differences.
Input/configuration/extraction/output failures exit **2**. Capture failures produce
`status: "error"` and structured `errors`, also in `--out` when writable; they
never produce `equal`. Parser usage errors go to stderr. A failure in one capture
stops comparison; no partial deltas are presented as a complete result.

```sh
# Require sku (a top-level string) on every entity; there is no fallback to @id.
qawrench structured-data old.html new.html \
  --base-url https://fixture.test/product --identity-field sku --fail-on-diff

# Explicitly ignore ordering and duplicates only at these entity-relative arrays.
qawrench structured-data old.html new.html \
  --base-url https://fixture.test/ --set-valued /sameAs --set-valued /keywords

# Compare only OpenGraph. Unselected JSON-LD is deliberately not validated.
qawrench structured-data old.html new.html \
  --base-url https://fixture.test/ --syntax opengraph
```

Examples with `old.html`/`new.html` above require your own local captures. No URL
fetching is available. The supplied example uses `/image` if you want to try
`--set-valued` with the shipped files. Unmatched pointers or pointers to nonarrays
fail instead of quietly ignoring a misspelled policy. `--syntax` is repeatable;
defaults to both `json-ld` and `opengraph`. `--syntax rdfa` explicitly fails.
There is no script execution, production resource mutation, persistent server,
or separate dry-run mode; writes are explicitly requested reports or baselines.

## Saved comparison baselines and coverage

Save a reviewed pair's exact differences as accepted noise, then check later
captures against the **same reference HTML** and policy. These commands use the
shipped synthetic pair; no network or production data is involved:

```sh
# Save the two known price/title deltas. Still prints the unfiltered comparison.
qawrench structured-data examples/structured_data/raw.html examples/structured_data/rendered.html \
  --base-url https://fixture.test/ --save-baseline /tmp/baseline.json

# Same two deltas are accepted: status equal, change_count 0, exit 0.
qawrench structured-data examples/structured_data/raw.html examples/structured_data/rendered.html \
  --base-url https://fixture.test/ --baseline /tmp/baseline.json --fail-on-diff
```

For this example the second report has `observed_change_count: 2`,
`accepted_change_count: 2`, `changes: []`, and the two original deltas (with
provenance) in `accepted_changes`. Changing the rendered price from `14.00` to
`15.00` leaves one unaccepted `/offers/price` delta and exits **1** with
`--fail-on-diff`; the unchanged title delta stays accepted. Exact acceptance
includes syntax, scope, entity, pointer, kind, and typed old/new values, including
absence versus null. It ignores source filenames/lines. A resolved difference
need not recur. Normalization runs before exact acceptance.

The JSON baseline has `baseline_version: 1`, tool/extractor versions, base URL,
full policy, both original capture SHA-256 hashes and coverage, and
`accepted_changes`. The reference hash and policy must match exactly; the later
capture hash is retained as historical provenance, not required to match future
captures. A changed reference, policy, URL, or extractor returns
`baseline_mismatch` (exit **2**). Baselines are local, unsigned, explicitly
trusted acceptance files; hashes identify inputs, not authorship. Rebaseline
explicitly with `--save-baseline`; it cannot be combined with `--baseline`.

Every successful report now includes `captures.old.coverage` and
`captures.new.coverage` for selected syntaxes only:

- JSON-LD: the exact XPath, candidate/selected script counts, skipped type
  mismatches with source lines, extracted root/entity/graph-wrapper counts, and
  resolved entity identities, their literal `entity_types`, and top-level
  `entity_nonblank_properties`. Candidates are scripts whose type contains
  `ld+json` ignoring case; selection still requires the exact upstream MIME type.
- OpenGraph: the direct-head property/content selector and namespace rule,
  candidate/selected/extracted-value/block counts, per-property value counts,
  and skipped metadata with reasons/lines (`name_attribute`, `missing_content`,
  `outside_head`, `unknown_namespace`). Candidates are all meta tags having a
  `property` or `name` attribute; ordinary named metadata may appear as skipped.

Independent preflight verifies that the tested extruct output preserves eligible
OpenGraph properties, repeats, order and namespaces, just as strict JSON-LD
preflight checks its extracted roots. An unexpectedly empty or incomplete
extractor result raises `extraction_failed`, never `equal`.

Saving a baseline rejects any decrease from reference to later capture in
selected scripts/values, extracted roots/entities/wrappers/OG blocks, existing
entity identities, or per-OG-property value counts. Rechecking enforces these
floors against each corresponding saved capture **before accepting deltas**.
For example, deleting a valid `[]` JSON-LD script produces no entity delta but
fails with `coverage_regression` (exit **2**); replacing an entity or OG property
with another also fails despite unchanged totals. Errors include the current
capture's coverage diagnostics. A failed extraction/check cannot update the
baseline. Baseline read/schema failures return `invalid_baseline`; write failures
return `baseline_io`.

Coverage is deliberately conservative: merging scripts, removing duplicate OG
values, or intentionally deleting entities requires reviewing new captures.
There is no coverage-loss override. Counts/identity/property floors do not prove
stable DOM block identity or full semantic coverage; field contents still use
exact deltas. Unsupported selectors remain outside comparison scope, and an
explicitly saved empty pair establishes zero floors unless minimums are requested
as below. Without a baseline,
successful empty comparisons remain valid and metadata removals remain ordinary
deltas, with coverage visible for inspection.

To prevent an accidentally empty initial capture from establishing those floors,
require minimum coverage explicitly when saving:

```sh
qawrench structured-data examples/structured_data/raw.html examples/structured_data/rendered.html \
  --base-url https://fixture.test/ --save-baseline /tmp/required-baseline.json \
  --minimum-coverage json-ld.entities 1 --minimum-coverage opengraph.selected 1

# Saved requirements apply automatically; do not repeat the creation flags.
qawrench structured-data examples/structured_data/raw.html examples/structured_data/rendered.html \
  --base-url https://fixture.test/ --baseline /tmp/required-baseline.json --fail-on-diff
```

Each repeatable `--minimum-coverage METRIC COUNT` requires **both captures** to
meet a nonnegative integer floor. It requires `--save-baseline`; it cannot be
used with plain comparisons or `--baseline`. Available metrics:

| Syntax | Metric names (prefix each with the syntax and `.`) |
| --- | --- |
| `json-ld` | `selected`, `extracted_roots`, `entities`, `graph_wrappers` |
| `opengraph` | `selected`, `extracted_values`, `blocks` |

For example, `json-ld.selected 1` requires one eligible script, but a script
containing `[]` meets that floor with zero entities. Use `json-ld.entities 1`
to require an identified entity. `opengraph.selected 1` requires an eligible
property/content meta node; repeated properties count as separate values, not
distinct property names. Candidate/skipped nodes do not count: scripts with
`type="Application/LD+JSON"` or metadata with `name="og:title"` cannot satisfy
these floors. Floors cover only explicitly requested metrics; choosing a
JSON-LD floor does not impose an OpenGraph floor.

An unmet floor returns `minimum_coverage`, exit **2**, with no deltas and no
baseline creation/replacement. Error provenance lists each failing capture's
file, full selector coverage and `unmet` entries with `metric`, `required`, and
`observed`. `--out` and Markdown retain these diagnostics. Unknown metrics,
unselected syntaxes, repeated CLI metrics and invalid counts return
`invalid_config` (exit **2**). A zero floor is valid and imposes no constraint.

The library equivalent is
`make_baseline(report, minimum_coverage={"json-ld.entities": 1})`; values must be
exact nonnegative Python integers (booleans, floats and strings fail). Nonempty
requirements are stored in the version-1 baseline's optional top-level
`minimum_coverage` object, separate from comparison policy. The reader validates
them against the saved captures (`invalid_baseline` on malformed requirements or
saved shortfalls), then replay checks the current captures before accepting
deltas. Existing hash/policy and per-capture coverage guards still apply. Missing
requirements in older baselines mean no explicit minimums. Omitting the option,
or passing `None`/`{}` to the library, preserves prior zero-floor behavior. These
are opt-in presence/count guards, not proof that all expected metadata exists.

Aggregate OpenGraph totals cannot require a particular property. To require a
title, add a named value-count floor when saving:

```sh
qawrench structured-data examples/structured_data/raw.html examples/structured_data/rendered.html \
  --base-url https://fixture.test/ --save-baseline /tmp/title-baseline.json \
  --minimum-coverage json-ld.selected 1 --minimum-og-property og:title 1

qawrench structured-data examples/structured_data/raw.html examples/structured_data/rendered.html \
  --base-url https://fixture.test/ --baseline /tmp/title-baseline.json --fail-on-diff
```

`--minimum-og-property PROPERTY COUNT` is repeatable for distinct property names
and requires `--save-baseline` and selected `opengraph` syntax. Both captures
must meet every floor. Names are exact, case-sensitive extracted property keys,
nonempty and without whitespace; no trimming or prefix allowlist is applied.
Declared custom namespace properties are supported. Unknown/unselected property
names have an observed count of zero. Counts are nonnegative integers; a zero
floor imposes no constraint. Repeated values count separately, including equal
duplicates: two eligible `og:title` nodes meet `og:title 2`. A capture containing
only `og:image` fails `og:title 1`, even though its OG total is nonzero. Skipped
`name`/body/missing-content metadata cannot satisfy the floor.

Named requirements combine with `--minimum-coverage`. Unmet requirements return
`minimum_coverage` (exit **2**, no deltas or baseline creation/replacement), with
each failing capture's file and full selector coverage. Named `unmet` entries use
`{"property":"og:title","required":1,"observed":0}`; aggregate entries retain
`metric`. Entries are ordered by metric, then property name. JSON and Markdown
retain both kinds. Invalid names, counts, duplicate CLI names or flag/syntax
combinations return `invalid_config` (exit **2**).

The library argument is `make_baseline(report, minimum_og_properties={"og:title": 1})`,
with exact nonnegative Python integers (no booleans, floats or strings). Nonempty
maps persist as optional top-level `minimum_og_properties` in baseline version 1,
separate from policy and aggregate minimums. The reader validates saved floors
and counts (`invalid_baseline` on malformed maps or saved shortfalls); replay
automatically enforces them before accepting deltas. Missing requirements,
`None` and `{}` retain prior behavior. Existing baseline variants remain readable;
older tool versions reject this extension. These guards count extracted values,
including empty strings; they do not certify meaningful property contents.

Entity totals likewise cannot require a particular JSON-LD entity. Require exact
entity identities when saving:

```sh
qawrench structured-data examples/structured_data/raw.html examples/structured_data/rendered.html \
  --base-url https://fixture.test/ --save-baseline /tmp/product-baseline.json \
  --require-jsonld-entity https://fixture.test/fixture/product

qawrench structured-data examples/structured_data/raw.html examples/structured_data/rendered.html \
  --base-url https://fixture.test/ --baseline /tmp/product-baseline.json --fail-on-diff
```

`--require-jsonld-entity ID` repeats for distinct identities, requires
`--save-baseline` and selected `json-ld` syntax, and checks **both captures**.
Match against the exact keys in `coverage.json-ld.entity_ids`: relative `@id`
values resolve against `--base-url` during extraction, blank nodes retain their
literal `_:...` key, and `--identity-field sku` uses the exact SKU string.
Requirement strings are **never trimmed, resolved or otherwise normalized**.
For an extracted `@id="../product"` under `https://fixture.test/catalog/page`,
require `https://fixture.test/product`, not `../product`. With a custom identity
field, whitespace and case in its key remain significant. Empty or whitespace-only
requirements and duplicate exact CLI identities return `invalid_config` (exit **2**).

An unrelated entity cannot satisfy the requirement even with nonzero entity
totals. Missing identities return `minimum_coverage` (exit **2**, no deltas or
baseline creation/replacement). Each failing capture includes its file, full
selector coverage and `unmet` entries such as
`{"entity":"https://fixture.test/product","required":1,"observed":0}` in JSON
and Markdown. Requirements combine with both minimum-count options; diagnostics
list metrics, total OG properties, nonblank OG properties, identities, then
entity/type pairs, entity/nonblank-property pairs, entity/nonempty-array pairs,
then entity/array-nonblank-string and entity/array-distinct-nonblank-string pairs,
sorted within each category.
Skipped type-mismatched scripts and empty graph wrappers do not supply identities.

The library argument is
`make_baseline(report, required_jsonld_entities=["https://fixture.test/product"])`.
It accepts a list of unique nonempty, non-whitespace-only strings; `None`/`[]`
preserve prior behavior. Nonempty lists persist sorted in the optional top-level
`required_jsonld_entities` field of baseline version 1. Missing/empty lists retain
backwards compatibility here; older readers reject the extension. Malformed or
unselected requirements and saved capture shortfalls return `invalid_baseline`.
Replay enforces saved requirements automatically before accepting deltas, alongside
existing hash/policy/coverage checks. These are identity-presence guards; they
impose no entity type, property content or semantic completeness requirement.

Property multiplicity counts empty values too. To require nonblank values while
establishing a baseline:

```sh
qawrench structured-data examples/structured_data/raw.html examples/structured_data/rendered.html \
  --base-url https://fixture.test/ --save-baseline /tmp/nonblank-baseline.json \
  --minimum-og-nonblank-property og:title 1

qawrench structured-data examples/structured_data/raw.html examples/structured_data/rendered.html \
  --base-url https://fixture.test/ --baseline /tmp/nonblank-baseline.json --fail-on-diff
```

`--minimum-og-nonblank-property PROPERTY COUNT` repeats for distinct exact,
case-sensitive property names and requires `--save-baseline` and selected
`opengraph` syntax. Names must be nonempty with no whitespace; counts must be
nonnegative integers. Duplicate entries, invalid names/counts and unselected
syntax return `invalid_config` (exit **2**). It combines with all other minimum
options, including a total and nonblank floor for the same property.

Only eligible extracted values whose Python `str.strip()` is nonempty count.
Repeated identical nonblank values count separately. HTML character references
are decoded by the existing parser: empty strings, spaces, tabs, newlines, NBSP
and other Python Unicode whitespace do not count; zero-width space does count.
Custom declared namespaces work. Skipped candidates, missing `content`, body
metadata and differently cased keys cannot satisfy a floor. Comparison values,
their order and deltas stay unchanged; there is no OG value normalization.

Both captures must meet the requested floors before saving. Replay uses the
saved floors automatically before accepting deltas. Shortfalls return
`minimum_coverage` (exit **2**, no deltas or baseline creation/replacement), with
per-capture file, full coverage/selector diagnostics and `unmet` entries such as
`{"property":"og:title","value_filter":"nonblank","required":1,"observed":0}`
in JSON and Markdown. A nonblank floor asserts presence of nonblank values,
without certifying meaningful text, URLs or semantic completeness.

The library argument is
`make_baseline(report, minimum_og_nonblank_properties={"og:title": 1})`.
`None`/`{}` retain prior behavior; integer values exclude bool. Nonempty maps
persist sorted as optional top-level `minimum_og_nonblank_properties` in baseline
version 1, separate from comparison policy. New captures report
`coverage.opengraph.nonblank_property_counts` for every extracted property,
including zero counts. Present maps must have exactly the total-count property
keys and integer values between zero and the corresponding total.

Historical baselines without those counts remain readable without nonempty
nonblank requirements. Adding a requirement (even a zero floor) to such a saved
baseline returns `invalid_baseline`; counts are never inferred from totals.
Malformed requirements/counts and saved shortfalls also return `invalid_baseline`.
The library rejects nonblank requirements on historical reports lacking counts
with `invalid_config`. Explicitly rebaseline from reviewed captures to add this
guard. Nonblank counts do **not** establish automatic coverage-loss floors;
existing baselines keep their prior acceptance behavior. Older readers reject
the new top-level requirement extension.

To require a literal JSON-LD type for a named entity when saving:

```sh
qawrench structured-data examples/structured_data/raw.html examples/structured_data/rendered.html \
  --base-url https://fixture.test/ --save-baseline /tmp/type-baseline.json \
  --require-jsonld-type https://fixture.test/fixture/product Product

qawrench structured-data examples/structured_data/raw.html examples/structured_data/rendered.html \
  --base-url https://fixture.test/ --baseline /tmp/type-baseline.json --fail-on-diff
```

`--require-jsonld-type ID TYPE` requires `--save-baseline` and selected `json-ld`
syntax. Repeat for distinct identity/type pairs; multiple types for one identity
must **all** be present. This implicitly requires that entity, without needing
`--require-jsonld-entity`. Identity matching follows the exact resolved/custom
keys described above. Both arguments must be nonblank strings; whitespace and
case remain significant. Duplicate pairs, blank arguments and unselected syntax
return `invalid_config` (exit **2**). All other minimum options can be combined.

Match the original extracted entity's literal `@type`, before any comparison
normalization: a nonblank string or an array consisting entirely of nonblank
strings. Repeated source types count once for membership. Missing `@type`, null,
numbers, objects, nested/mixed arrays and arrays containing blank strings supply
no types. Such values still participate in ordinary comparison; this guard does
not change input acceptance, array order/multiplicity or deltas. A nested
reference, skipped script or unrelated entity cannot supply the required type.
There is no context/alias expansion or URI equivalence: `Product`, `product`,
`https://schema.org/Product` and ` Product ` are distinct literals. This is a
presence guard, without type-vocabulary or rich-result validation.

Both initial captures and both current replay captures must meet saved
requirements before persistence or delta acceptance. Shortfalls return
`minimum_coverage` (exit **2**, no deltas or baseline creation/replacement), with
per-capture file, full selector coverage and `unmet` entries such as
`{"entity":"https://fixture.test/product","type":"Product","required":1,"observed":0,"observed_types":["Thing"]}`
in JSON and Markdown. Hash, policy and existing coverage checks still apply.

The library argument is
`make_baseline(report, required_jsonld_types={"https://fixture.test/product": ["Product"]})`.
It accepts a dict of exact nonblank identity strings to nonempty lists of unique
nonblank type strings. `None`/`{}` retain prior behavior. Nonempty maps persist
as optional top-level `required_jsonld_types` in baseline version 1, with sorted
keys/type lists, separate from comparison policy. New captures include
`coverage.json-ld.entity_types` for every extracted identity, with sorted unique
literal type lists (including empty lists). Present coverage maps must have
exactly the entity identity keys and lists of distinct nonblank strings.

Historical baselines lacking type coverage remain readable without nonempty
type requirements. Adding requirements to them returns `invalid_baseline`;
the library returns `invalid_config` for historical reports lacking these
counts. Types are never inferred from identity presence. Malformed saved
requirements/coverage and saved shortfalls also return `invalid_baseline`.
Explicitly rebaseline from reviewed captures to add the guard. Type coverage
adds no automatic loss floors, so existing baselines retain their acceptance
behavior. Older readers reject the top-level requirement extension.

To require a nonblank top-level JSON-LD string for a named entity:

```sh
qawrench structured-data examples/structured_data/raw.html examples/structured_data/rendered.html \
  --base-url https://fixture.test/ --save-baseline /tmp/name-baseline.json \
  --require-jsonld-nonblank-property https://fixture.test/fixture/product name

qawrench structured-data examples/structured_data/raw.html examples/structured_data/rendered.html \
  --base-url https://fixture.test/ --baseline /tmp/name-baseline.json --fail-on-diff
```

`--require-jsonld-nonblank-property ID PROPERTY` requires `--save-baseline` and
selected `json-ld` syntax. Repeat for distinct pairs; all requested fields must
pass on their named entities in **both initial and both current replay captures**.
This implicitly requires the entity. Identity matching uses the exact extracted
keys described above; neither argument is trimmed or normalized. Both must be
nonblank strings. Duplicate exact pairs, blank arguments, unselected syntax or
use without saving return `invalid_config` (exit **2**).

`PROPERTY` is an exact, case-sensitive top-level field name, **not a JSON pointer**.
Slashes, tildes, `*`, surrounding whitespace and JSON-LD keywords are literal
parts of a name. Only original extracted string values whose Python `str.strip()`
is nonempty qualify, before comparison normalization. Surrounding Unicode
whitespace is allowed around nonblank text; whitespace-only strings, missing
fields, null, numbers, booleans, objects (including `{"@value":"Mug"}`) and arrays
fail. Zero-width space counts as nonblank under Python's rules. Nested fields,
unrelated entities, skipped scripts and context aliases cannot supply a field.
There is no recursive search, coercion or context expansion. Comparison values,
normalization and deltas retain their existing behavior. Nonblank presence does
not certify meaningful text or semantic completeness.

A shortfall returns `minimum_coverage` (exit **2**, no deltas or baseline
creation/replacement), before accepting baseline deltas. JSON and Markdown
include the failing capture, file, full selector coverage and `unmet` entries:
`{"entity":"https://fixture.test/product","property":"name","value_filter":"nonblank","required":1,"observed":0}`.
These requirements combine with all existing guards; hash, policy and coverage
checks still apply.

The library argument is
`make_baseline(report, required_jsonld_nonblank_properties={"https://fixture.test/product": ["name"]})`.
It accepts a dict of exact nonblank identity strings to nonempty lists of unique
nonblank property strings. `None`/`{}` preserve prior behavior. Nonempty maps
persist sorted as optional top-level `required_jsonld_nonblank_properties` in
baseline version 1, separate from comparison policy. Replay automatically uses
the saved requirements. New captures include
`coverage.json-ld.entity_nonblank_properties`: every extracted identity maps to
sorted unique names of its qualifying top-level string fields. Present coverage
maps must have exactly the identity keys and lists of distinct string names
(including blank names if present in the extracted object).

Historical baselines lacking this coverage remain readable without nonempty
requirements. Adding requirements to them returns `invalid_baseline`; the
library returns `invalid_config` for historical reports lacking coverage. Field
presence is never inferred from identities, types or accepted deltas. Malformed
saved requirements/coverage and saved shortfalls return `invalid_baseline`.
Rebaseline from reviewed captures to add the guard. This coverage introduces
**no automatic loss floors**. Older readers reject the top-level requirement
extension.

To require a nonempty top-level JSON-LD array for a named entity:

```sh
qawrench structured-data examples/structured_data/raw.html examples/structured_data/rendered.html \
  --base-url https://fixture.test/ --save-baseline /tmp/image-baseline.json \
  --require-jsonld-nonempty-array https://fixture.test/fixture/product image

qawrench structured-data examples/structured_data/raw.html examples/structured_data/rendered.html \
  --base-url https://fixture.test/ --baseline /tmp/image-baseline.json --fail-on-diff
```

`--require-jsonld-nonempty-array ID PROPERTY` requires `--save-baseline` and
selected `json-ld` syntax. Repeat for distinct pairs; every requested array must
exist on its named entity in **both initial and both current replay captures**.
Both arguments must be nonblank strings, matched exactly without trimming or
normalization. Duplicate pairs, blank arguments, unselected syntax or use without
saving return `invalid_config` (exit **2**). Identity matching uses extracted keys
(resolved `@id`, literal blank nodes or custom identity values) as above.

`PROPERTY` is a literal case-sensitive top-level name, including any slashes,
tildes, `*` or surrounding whitespace; it is not a pointer or wildcard. Only an
**original extracted list of length at least one** qualifies, before string
normalization or set-valued comparison. Missing fields, `[]`, null, strings,
numbers, booleans and objects fail. `[null]`, `[""]`, `[{}]` and `[[]]` pass presence
only: element validity, image URLs and semantic completeness are not certified.
There is no recursive lookup, string-to-array coercion or context expansion.
Comparison values, ordering, multiplicity, normalization and deltas are unchanged.

Shortfalls return `minimum_coverage` (exit **2**, `changes: []`) before baseline
creation/replacement or replay delta acceptance. JSON/Markdown include each
failing capture, file, full selector coverage and `unmet` entries, for example:
`{"entity":"https://fixture.test/product","property":"image","value_filter":"nonempty-array","required":1,"observed":0,"observed_type":"array","observed_length":0}`.
`observed_type` is `missing` or a JSON type; `observed_length` appears for arrays.
Existing metric/OG/entity/type/nonblank requirements and hash/policy/coverage
guards still apply. An explicitly accepted array-removal delta cannot bypass
this requirement.

The library argument is
`make_baseline(report, required_jsonld_nonempty_arrays={"https://fixture.test/product": ["image"]})`.
It accepts a dict of exact nonblank identity strings to nonempty lists of unique
nonblank property strings. `None`/`{}` preserve prior behavior. Nonempty maps
persist sorted as optional top-level `required_jsonld_nonempty_arrays` in baseline
version 1, outside comparison policy; replay uses the saved requirements.

New JSON-LD capture coverage includes `entity_property_shapes`: every extracted
identity maps to its exact top-level names and shallow original shapes. Arrays
have `{"type":"array","length":N}` (nonnegative integer, not boolean); other
values have only `type` (`null`, `boolean`, `number`, `string` or `object`). No
element values are stored in this metadata. Present maps require exactly the
captured identity keys and valid shapes. This metadata creates **no implicit
loss floors**, including array-length floors. Historical baselines without it
remain readable without nonempty requirements. Adding requirements to them
returns `invalid_baseline`; the library returns `invalid_config` for historical
reports missing the coverage. Presence is never inferred from normalized deltas.
Malformed saved requirements/coverage or saved shortfalls return
`invalid_baseline`. Rebaseline from reviewed captures to add the guard. Older
readers reject the optional top-level requirement extension.

To require nonblank string elements in a named entity's top-level array:

```sh
qawrench structured-data examples/structured_data/raw.html examples/structured_data/rendered.html \
  --base-url https://fixture.test/ --save-baseline /tmp/image-strings-baseline.json \
  --minimum-jsonld-array-nonblank-string https://fixture.test/fixture/product image 1

qawrench structured-data examples/structured_data/raw.html examples/structured_data/rendered.html \
  --base-url https://fixture.test/ --baseline /tmp/image-strings-baseline.json --fail-on-diff
```

`--minimum-jsonld-array-nonblank-string ID PROPERTY COUNT` requires
`--save-baseline` and selected `json-ld` syntax. Repeat for distinct exact
identity/property pairs. Both strings must be nonblank, and `COUNT` must parse
as a nonnegative integer. Duplicate pairs (even with different counts), blank
arguments, invalid counts, unselected syntax or use without saving return
`invalid_config` (exit **2**). Replay uses the saved requirements.

Only immediate elements of the **original extracted list** that are strings
with nonempty Python `str.strip()` count. Repeated identical strings count
separately, before set-valued deduplication or comparison normalization.
`[null]`, `[""]`, `[{}]`, `[[]]` and `["", " ", {}]` have zero qualifying elements;
`[" A ", "A", null, {}]` has two. Zero-width space counts as nonblank under
Python's rules. Mixed arrays are allowed; nonstrings contribute zero. No nested
search, coercion, context expansion, URL validation or comparison-value change
occurs. Identity and literal top-level property matching follows the exact rules
above, including whitespace/case and literal `/`, `~` and `*` in names.

**A zero floor still requires an array**: `[]` passes zero, but missing fields,
null, strings, numbers, booleans and objects fail even with zero requested.
All requirements apply to **both initial and both current replay captures**.
Shortfalls return `minimum_coverage` (exit **2**, `changes: []`) before baseline
creation/replacement or delta acceptance. Existing baseline bytes remain intact.
JSON and Markdown include the failing capture/file, full selector coverage and,
for example,
`{"entity":"https://fixture.test/product","property":"image","value_filter":"array-nonblank-string","required":1,"observed":0,"observed_type":"array"}`.
`observed` counts qualifying elements; `observed_type` is `missing` or a JSON
type. These counts assert string presence, not image validity or completeness.
All existing metric/OG/entity/type/nonblank/array/hash/policy/coverage guards
continue to apply, and accepted deltas cannot bypass the floor.

The library argument is
`make_baseline(report, minimum_jsonld_array_nonblank_strings={"https://fixture.test/product": {"image": 1}})`.
It accepts exact nonblank identities mapped to nonempty dicts of exact nonblank
properties and nonnegative integer counts, excluding booleans. `None`/`{}`
preserve prior behavior; nested empty dicts are invalid. Nonempty maps persist
sorted as optional top-level `minimum_jsonld_array_nonblank_strings` in baseline
version 1, outside comparison policy.

JSON-LD coverage adds optional `entity_array_nonblank_string_counts`: every
captured identity maps to exactly its top-level array names and their original
qualifying counts. Present counts must be nonnegative integers (not booleans),
no greater than the corresponding original `entity_property_shapes` array
length; that shape map must also be present. This metadata adds **no implicit
floors**. Historical baselines without element counts remain readable without
this requirement, including with the prior array-presence guard. Requirements
(even zero floors) cannot be added to historical captures without counts:
`invalid_baseline` on replay or `invalid_config` for historical library reports.
Malformed saved requirements/counts/shapes or saved shortfalls return
`invalid_baseline`. Counts are never inferred from normalized deltas. Rebaseline
from reviewed captures to add this guard. Older readers reject the optional
top-level requirement extension.

To require **distinct** nonblank string values in the original top-level array:

```sh
qawrench structured-data examples/structured_data/raw.html examples/structured_data/rendered.html \
  --base-url https://fixture.test/ --save-baseline /tmp/distinct-images-baseline.json \
  --minimum-jsonld-array-distinct-nonblank-string https://fixture.test/fixture/product image 1

qawrench structured-data examples/structured_data/raw.html examples/structured_data/rendered.html \
  --base-url https://fixture.test/ --baseline /tmp/distinct-images-baseline.json --fail-on-diff
```

`--minimum-jsonld-array-distinct-nonblank-string ID PROPERTY COUNT` repeats for
exact unique identity/property pairs and requires `--save-baseline` with selected
`json-ld` syntax. Keys must be nonblank exact strings; counts must be nonnegative
integers. Duplicate pairs (even with different counts), blank arguments, invalid
counts and invalid flag/syntax combinations return `invalid_config` (exit **2**).
The library equivalent is
`make_baseline(report, minimum_jsonld_array_distinct_nonblank_strings={"https://fixture.test/product": {"image": 2}})`.
Library floors must be Python integers excluding booleans; nested empty maps are
invalid. `None`/`{}` preserve prior behavior. Nonempty maps persist sorted in the
optional top-level `minimum_jsonld_array_distinct_nonblank_strings` baseline-v1
field, outside comparison policy. Replay uses saved requirements automatically.

Count unique **original immediate string values** whose Python `str.strip()` is
nonempty. Distinctness compares full original strings exactly, before comparison
normalization or set deduplication: `["front.png", "front.png"]` observes **1**;
`["front.png", "back.png", null, {}]` observes **2**. `" A "`, `"A"` and `"a"`
are three distinct values; Unicode forms are not normalized. Whitespace-only
strings do not count; zero-width space does. Mixed nonstrings are ignored.
Identity and literal top-level property rules above apply; no recursive search,
coercion, context expansion, URL validation or comparison-value changes occur.
The existing multiplicity floor continues to count repeated strings separately,
and both floors can be requested for the same property.

A zero floor still requires an original array (`[]` passes; absent/nonarray fails).
Both initial captures and both current replay captures must meet the floor before
baseline persistence or exact-delta acceptance. Shortfalls return
`minimum_coverage` (exit **2**, `changes: []`), preserving existing baseline bytes.
JSON and Markdown retain each failing capture/file, full selector coverage and
`unmet` entries such as
`{"entity":"https://fixture.test/product","property":"image","value_filter":"array-distinct-nonblank-string","required":2,"observed":1,"observed_type":"array"}`.
`observed` is the distinct qualifying count; `observed_type` is `missing` or a JSON
type. All earlier coverage, requirement, hash and policy guards continue to apply.

Coverage adds optional `entity_array_distinct_nonblank_string_counts`, mapping
every captured identity to exactly its top-level array names and original distinct
counts. Present counts must be nonnegative integers excluding booleans, bounded
by the matching original `entity_property_shapes` array lengths; shapes must be
present. When multiplicity counts are present, distinct counts cannot exceed them
and must be zero exactly when multiplicity is zero. These summaries add **no
implicit floors**. Historical captures without distinct counts remain usable
without this requirement; required counts (including zero floors) are never
inferred from multiplicity, shapes or deltas. Missing required historical metadata
returns `invalid_config` for library reports or `invalid_baseline` on replay.
Malformed saved requirements/counts/shapes or saved shortfalls also return
`invalid_baseline`. Rebaseline reviewed captures to add the guard. Older readers
reject the optional top-level requirement extension. Distinct counts assert
lexical diversity, not image validity or semantic completeness; collection uses
memory proportional to the distinct immediate strings in an array.

### Require original JSON-LD array string members

Use `--require-jsonld-array-string ID PROPERTY MEMBER` when saving a baseline,
or `make_baseline(report, required_jsonld_array_strings={ID: {PROPERTY: [MEMBER, ...]}})`.
Repeat distinct triples to require **every** member, including multiple members
of the same array. For two saved captures containing
`{"@id":"https://fixture.test/product","image":["front.png","back.png"]}`:

```sh
qawrench structured-data old.html new.html --base-url https://fixture.test/ \
  --save-baseline accepted.json \
  --require-jsonld-array-string https://fixture.test/product image front.png \
  --require-jsonld-array-string https://fixture.test/product image back.png
qawrench structured-data old.html new.html --base-url https://fixture.test/ \
  --baseline accepted.json --fail-on-diff
```

Identity and property names match exactly as above; property names are literal
top-level keys, not pointers. Required members must be strings whose Python
`str.strip()` is nonempty, but comparison uses the **complete original string**
without trimming, case folding or Unicode normalization. `" back.png "` differs
from `"back.png"`. Immediate original array strings only: nested strings,
`@value` objects and coerced values do not match. Duplicates neither add nor
substitute members; mixed nonstrings are ignored. No context expansion, recursive
lookup, URL validation or changes to comparison values occur. Nonblank arbitrary
text and zero-width space qualify. Blank identity/property/member arguments,
duplicate triples, invalid library maps/lists and unselected `json-ld` syntax
return `invalid_config` (exit **2**). Flags require `--save-baseline`; replay
uses the saved optional baseline-v1 `required_jsonld_array_strings` requirements.

Both initial captures and both current replay captures must satisfy all members
before persistence or exact-delta acceptance. An absent/nonarray property or a
missing member returns `minimum_coverage` (exit **2**, `changes: []`), with no
baseline creation/replacement. For `["front.png","side.png"]` with required
`back.png`, each failing capture's JSON/Markdown diagnostics include full selector
coverage and an `unmet` entry:
`{"entity":"https://fixture.test/product","property":"image","member":"back.png","value_filter":"array-string-member","required":1,"observed":0,"observed_type":"array","observed_members":["front.png","side.png"]}`.
`observed_type` is `missing` or a JSON type. Existing shape/count/coverage, hash
and policy guards remain in force; normalization or accepted deltas cannot hide
a missing original member.

Optional coverage `entity_array_nonblank_string_members` maps every identity to
exactly its original top-level arrays, each containing a sorted unique list of
original nonblank strings. Saved lists must be duplicate-free and bounded by
original array lengths in required `entity_property_shapes`; when present,
distinct counts must equal list length, and multiplicity must bound it with
matching zero/nonzero status. These summaries add **no implicit floors**.
Historical baselines without members remain usable without this requirement.
Members are never inferred from counts, shapes or normalized deltas; adding the
requirement needs reviewed recapture. Missing required metadata yields
`invalid_config` for library reports or `invalid_baseline` on replay. Malformed
saved metadata/requirements and saved shortfalls yield `invalid_baseline`.
Older readers reject the optional requirement extension. Membership records
increase report/baseline size and memory with original distinct strings; this is
lexical membership, not semantic or image validity.

### Allow only original JSON-LD array string members

Use `--allow-jsonld-array-string ID PROPERTY MEMBER` with `--save-baseline`, or
`make_baseline(report, allowed_jsonld_array_strings={ID: {PROPERTY: [MEMBER, ...]}})`.
Repeat distinct triples to build an explicit allowed set for each exact named
entity and literal top-level property. For captures containing only `front.png`
and/or `back.png` in `image`:

```sh
qawrench structured-data old.html new.html --base-url https://fixture.test/ \
  --save-baseline accepted.json \
  --allow-jsonld-array-string https://fixture.test/product image front.png \
  --allow-jsonld-array-string https://fixture.test/product image back.png
qawrench structured-data old.html new.html --base-url https://fixture.test/ \
  --baseline accepted.json --fail-on-diff
```

This is a **subset constraint**: every immediate original nonblank string must
be allowed. Full original strings compare exactly before normalization or set
comparison: no trimming, case folding, Unicode normalization, nested lookup,
coercion, context expansion or URL validation. Python `str.strip()` defines
nonblank eligibility only. Duplicate original values do not matter; blank strings
and mixed nonstrings are ignored, including nested arrays and `@value` objects.
Empty original arrays pass. An allowlist implies **no required membership or
count floor**; combine the existing `--require-jsonld-array-string`, nonempty
array or count guards to require presence. Normal comparison values stay unchanged.

Both initial captures and both current replay captures must satisfy the guard
before persistence or exact-delta acceptance. With allowed `front.png`/`back.png`,
`["front.png","back.png","side.png","side.png"]` fails with `minimum_coverage`
(exit **2**, `changes: []`). JSON/Markdown includes each failing capture's selector
coverage and an `unmet` entry:
`{"entity":"https://fixture.test/product","property":"image","value_filter":"array-allowed-string-members","required":0,"observed":1,"observed_type":"array","unexpected_members":["side.png"],"allowed_members":["back.png","front.png"],"observed_members":["back.png","front.png","side.png"]}`.
Here `required: 0` means zero unexpected distinct strings; `observed` counts them.
Absent/nonarray properties also fail, with their `observed_type` (`missing` or
JSON type) and empty unexpected list. Failed creation/replacement/replay preserves
existing baseline bytes. Earlier hash/policy/coverage and explicit guards apply.

Nonblank exact identity/property/member arguments, selected `json-ld` syntax,
nonempty property maps and nonempty duplicate-free allowlists are required.
Duplicate CLI triples or malformed configuration return `invalid_config` (exit
**2**). The flag requires saving; replay uses optional baseline-v1
`allowed_jsonld_array_strings`. The library accepts `None`/an empty outer dict
as no constraint; that field is omitted on save. Saved null, malformed saved
requirements/metadata and saved subset failures return `invalid_baseline`.

The guard reuses `entity_array_nonblank_string_members` and
`entity_property_shapes` with the shape/count cross-checks above, adding no
metadata or implicit floors. Historical baselines remain readable without the
allowlist. Missing historical members/shapes cannot be inferred from counts or
deltas: adding the guard needs reviewed recapture (`invalid_config` for library
reports, `invalid_baseline` for saved captures lacking metadata). Older readers
reject this optional requirement extension. This string-only lexical constraint
does not validate image contents or all array element types.

### Limit original JSON-LD array nonblank string multiplicity

Use `--maximum-jsonld-array-nonblank-string ID PROPERTY COUNT` when saving, or
`make_baseline(report, maximum_jsonld_array_nonblank_strings={ID: {PROPERTY: COUNT}})`.
Repeat distinct identity/property pairs to set independent ceilings. For `image`
containing `["front.png", "back.png"]`:

```sh
qawrench structured-data old.html new.html --base-url https://fixture.test/ \
  --save-baseline accepted.json \
  --maximum-jsonld-array-nonblank-string https://fixture.test/product image 2 \
  --allow-jsonld-array-string https://fixture.test/product image front.png \
  --allow-jsonld-array-string https://fixture.test/product image back.png \
  --require-jsonld-array-string https://fixture.test/product image front.png \
  --require-jsonld-array-string https://fixture.test/product image back.png
qawrench structured-data old.html new.html --base-url https://fixture.test/ \
  --baseline accepted.json --fail-on-diff
```

This counts **immediate original nonblank strings, including repetitions**.
`["front.png", "front.png", "back.png"]` exceeds maximum 2 even though all strings
are allowed and all required members are present. Python `str.strip()` defines
eligibility without changing values. Blank strings and mixed nonstrings are
ignored, including nested arrays and `@value` objects. Zero-width space and
arbitrary nonblank text qualify. No recursion, coercion, context expansion, URL
validation or comparison-value changes occur. Normalization and set comparison
cannot hide original multiplicity. Maximum zero permits `[]` or an array with no
qualifying strings; absent/nonarray properties fail even at zero. A ceiling
implies no required membership or minimum; combine the existing guards as needed.

Both initial captures and both current replay captures must meet the ceiling
before persistence or exact-delta acceptance. Failure returns `maximum_coverage`
(exit **2**, `changes: []`), preserving baseline bytes and including each failing
capture's selector coverage plus an `unmet` entry, for example:
`{"entity":"https://fixture.test/product","property":"image","value_filter":"array-nonblank-string","maximum":2,"observed":3,"observed_type":"array"}`.
Nonarrays report their JSON type (or `missing`) with observed count zero.
Earlier hash/policy, coverage, shape/count/member and explicit minimum guards
remain in force and may reject independently.

Identities and literal top-level property names must be exact nonblank strings;
they are neither pointers nor expanded aliases. Require selected `json-ld` and a
nonnegative integer ceiling (not bool). Duplicate CLI pairs (even with the same
count), malformed maps, blank names and invalid counts return `invalid_config`
(exit **2**). Flags require `--save-baseline`; replay uses optional baseline-v1
`maximum_jsonld_array_nonblank_strings`. Library `None`/empty outer dict means no
constraint and is omitted from saved output. Saved null, malformed requirements,
metadata or a saved ceiling failure return `invalid_baseline`.

The guard reuses `entity_array_nonblank_string_counts` and
`entity_property_shapes`, with existing count/shape/member cross-checks. It adds
no metadata or implicit upper bounds. Historical baselines remain usable without
the option. Missing original counts/shapes cannot be inferred from distinct
members, lengths or deltas: adding the guard requires reviewed recapture
(`invalid_config` for library reports, `invalid_baseline` for saved captures).
Older readers reject this optional requirement extension. The ceiling applies
only to qualifying strings, not total elements or semantic content.

### Require every original JSON-LD array element to be a nonblank string

Use `--require-jsonld-array-all-nonblank-strings ID PROPERTY` when saving, or
`make_baseline(report, required_jsonld_array_all_nonblank_strings={ID: [PROPERTY]})`.
Repeat distinct identity/property pairs for multiple requirements. For captures
with `https://fixture.test/product` and `image=["front.png", "back.png"]`:

```sh
qawrench structured-data old.html new.html --base-url https://fixture.test/ \
  --save-baseline accepted.json \
  --require-jsonld-array-all-nonblank-strings https://fixture.test/product image
qawrench structured-data old.html new.html --base-url https://fixture.test/ \
  --baseline accepted.json --fail-on-diff
```

Every **original immediate element** must be a Python string whose `str.strip()`
is nonempty. `["front.png", null, {"@value":"back.png"}, " "]` fails even when
all existing allow/member/count/ceiling requirements pass. Nested arrays/objects,
numbers, booleans, null and blank strings fail; there is no recursion, coercion,
context expansion or URL validation. Zero-width space and arbitrary nonblank
text qualify. Original values are unchanged, and normalization/set comparison
cannot hide original elements or repetitions. The guard uses original qualifying
string **multiplicity**, which must equal original total array length; distinct
member counts cannot substitute.

**Empty arrays pass** this universal constraint. Combine a nonempty-array or
count requirement when presence is also required; those guards stay independent.
Missing entities/properties and nonarrays fail, including strings and `@list`
objects. Identity and literal top-level property names are exact nonblank strings,
without trimming, case folding or alias expansion; property names are not pointers.
Selected `json-ld` is required. Duplicate CLI pairs, blank names and malformed
maps/lists return `invalid_config` (exit **2**). Library maps require nonempty,
duplicate-free property lists; `None`/empty outer dict means no constraint.

Both initial captures and both current replay captures must satisfy the guard
before baseline creation/replacement or exact-delta acceptance. Failure returns
`minimum_coverage` (exit **2**, `changes: []`), preserves baseline bytes, and
includes each failing capture's full selector coverage plus an `unmet` entry:
`{"entity":"https://fixture.test/product","property":"image","value_filter":"array-all-nonblank-strings","required":"all","observed":1,"observed_type":"array","observed_length":4}`.
`observed` is original qualifying multiplicity. Nonarrays report their JSON type
(or `missing`), length `null` and qualifying count zero. Earlier hash/policy,
coverage, metadata validation and explicit guards remain in force.

Flags require `--save-baseline`; replay automatically enforces optional
baseline-v1 `required_jsonld_array_all_nonblank_strings`. Saved null, malformed
requirements/metadata and failing saved captures return `invalid_baseline`.
Existing original `entity_property_shapes` and `entity_array_nonblank_string_counts`
are reused with existing shape/count/member cross-checks. No new metadata,
implicit all-elements guard or coverage floor is added. Historical baselines
remain usable without this option. Missing original counts/shapes require reviewed
recapture to add it, even for `[]`: neither lengths, distinct members nor deltas
can supply inferred counts (`invalid_config` for library reports,
`invalid_baseline` for saved captures). Older readers reject this optional
requirement extension. This lexical constraint does not certify image content.

Baseline and report files are replaced atomically, separately (no multi-file
transaction). Destinations must differ from HTML inputs, each other and a loaded
baseline, including resolved symlinks. Baseline JSON is independent of the report
`--format`. `--save-baseline` prints the original comparison and retains its usual
exit policy, so saving a differing pair with `--fail-on-diff` still exits **1**.

## Per-field string normalization

String values stay lexical by default. Opt in per entity-relative RFC 6901
pointer, extended with array wildcards below, with `--normalize POINTER MODE`
(repeat for different fields), or the library argument
`normalize={"/name": "collapse-whitespace"}`. One mode per field:

| Mode | Operation | Example equal values |
| --- | --- | --- |
| `trim` | Python `str.strip()`: remove leading/trailing Unicode whitespace | `"  mug "`, `"mug"` |
| `collapse-whitespace` | Python `" ".join(value.split())`: trim and collapse Unicode whitespace runs to one ASCII space | `" Tea\n mug "`, `"Tea mug"` |
| `casefold` | Python `str.casefold()`: Unicode case folding, preserving whitespace | `"Straße"`, `"STRASSE"` |

The shipped synthetic pair makes normalization and remaining differences visible:

```sh
qawrench structured-data examples/structured_data/normalization-old.html examples/structured_data/normalization-new.html \
  --base-url https://fixture.test/catalog/page \
  --normalize /name collapse-whitespace --normalize /brand casefold \
  --set-valued /tags --out /tmp/normalized.json
```

`name` changes from `"  Tea\n mug "` to `"Tea mug"`, `brand` changes from
`"Straße"` to `"STRASSE"`, and `tags` changes order/duplicate count. Those compare
equal under these policies. The report still contains exactly these six changes
(common fields/provenance omitted):

```json
[
  {"kind":"changed", "path":"/description", "old":null, "new":""},
  {"kind":"removed", "path":"/note", "old":""},
  {"kind":"changed", "path":"/steps/@list/0", "old":"brew", "new":"serve"},
  {"kind":"changed", "path":"/steps/@list/1", "old":"serve", "new":"brew"},
  {"kind":"removed", "path":"/steps/@list/2", "old":"serve"},
  {"kind":"added", "path":"/summary", "new":null}
]
```

Normalization never fills an absent field, converts null to a string, or drops an
empty value. Machine readers distinguish absence by a missing `old`/`new` key,
JSON null by `null`, and empty string/array/object by `""`/`[]`/`{}`. Whitespace-only
strings intentionally become `""` under `trim` or `collapse-whitespace`.
Delta values are **normalized comparison values**; `policy.normalize` records
the modes, while capture digests/provenance locate the original input.

Policies apply to every JSON-LD entity, including added/removed entities.
They do not recurse into a selected container or apply to graph-wrapper metadata
or OpenGraph. Null targets remain null; other non-string
targets (including empty arrays/objects), unknown modes, malformed/unmatched
pointers, repeated CLI pointers, and targeting the top-level identity field fail
with `invalid_config` (CLI exit **2**, never `equal`). A pointer must exist in at
least one entity across the two captures; it may be absent elsewhere. String
normalization precedes set sorting/deduplication, so array indices address the
original extracted positions. No URL/number coercion, mode chaining,
Unicode NFC normalization, or context expansion is performed. Unicode behavior
follows the running Python version.

### Repeated values with array wildcards

Quote a whole `*` pointer token to normalize every array member, for example
`--normalize '/labels/*/@value' collapse-whitespace`. This turns
`[{"@value":" Tea\n mug "},{"@value":" Cup "}]` into comparison values
`[{"@value":"Tea mug"},{"@value":"Cup"}]`. It also supports string arrays
(`/tags/*`) and nested arrays (`/groups/*/labels/*/@value`). Other tokens keep
their exact RFC 6901 meaning, including `~0`/`~1` escapes and partial tokens
like `label*`. On an object, `*` retains its exact literal property-key meaning;
it never expands object properties. Wildcards are supported only for string
normalization; `--set-valued` still accepts exact pointers.

```sh
qawrench structured-data examples/structured_data/wildcard-old.html examples/structured_data/wildcard-new.html \
  --base-url https://fixture.test/catalog/page \
  --normalize '/labels/*/@value' collapse-whitespace \
  --normalize '/steps/@list/*/@value' trim --fail-on-diff
```

This shipped synthetic pair exits **1** with exactly three ordered-list deltas:
`changed /steps/@list/0/@value` (`brew` to `serve`),
`changed /steps/@list/1/@value` (`serve` to `brew`), and
`removed /steps/@list/2` (`{"@value":"serve"}`). Label whitespace compares
equal. Normalization inside `@list` preserves order and duplicate counts.

Successful JSON/Markdown reports include `normalization_matches`, mapping each
selector to `old` and `new` lists of `{"entity": "resolved identity", "path":
"concrete entity-relative pointer"}`. Lists use sorted entity identities and
numeric original array order, before set sorting/deduplication. Null counts as a
match and remains null. Missing properties/indices (including an absent
`@value`) are skipped; empty arrays contribute no matches. Every selector must
match at least one value across both captures, or it fails with `invalid_config`.

A reached wildcard requires an array or an object with a literal `*` key.
Scalars encountered inside an expanded branch before its suffix ends, and
non-string/non-null selected values, fail with `invalid_config` (exit **2**).
Two selectors resolving to the same value also fail, even with the same mode;
there is no precedence or mode chaining. An expanded target cannot also have a
set-valued policy. Value errors identify the selector and concrete pointer with
entity/source provenance. The first error is deterministic: sorted entity
identities, old then new per entity, sorted selectors, numeric array indices.

Explicit `@list` arrays always retain order and duplicate counts. For example,
`--set-valued /steps/@list` fails with `invalid_config` rather than treating the
fixture's reordered list as equal. Sets elsewhere may contain ordered list
objects, whose internal order remains intact. Context-defined list aliases and
`@container` semantics are not resolved; this guard recognizes literal `@list`.

## Supported comparison policy

- **Input:** strict UTF-8 (optional UTF-8 BOM). Invalid bytes and NULs fail.
  HTML is parsed with lxml's recovering HTML parser, so this is not an HTML
  conformance validator. Script selector matches extruct 0.18.0 exactly:
  `script[type="application/ld+json"]` (case-sensitive attribute value).
- **JSON-LD:** strict JSON only; comments, trailing commas, duplicate object keys,
  non-finite numbers, lone Unicode surrogates, scalar roots and empty object
  roots fail. Roots can be identified objects or arrays of such objects.
  Empty root arrays are valid and contribute zero entities. Preflight validation
  prevents extruct's permissive decoder or falsy-item filtering from losing data;
  extraction still calls `extruct.extract(..., errors="strict", uniform=False)`.
- **Graphs:** flatten each top-level `@graph` array into the entity index. A
  wrapper supports only `@graph` and optional `@context`; named graphs/additional
  wrapper fields fail explicitly. Wrapper metadata is retained in an ordered
  list and compared separately (`scope: "graph_metadata"`); `/0/@context` addresses
  its first wrapper. This preserves even empty-graph contexts and does not merge
  them with member-local contexts. Wrapper boundaries/order can therefore produce
  differences. Nested `@graph` values remain ordinary nested JSON properties.
- **Identity:** every flattened entity needs a unique nonempty string `@id`, or
  the exact field named by `--identity-field`. No positional matching or type/name
  guessing. Duplicates (including relative-ID collisions) and missing/array-valued
  identities fail with file, script index, line and source JSON pointer.
- **Relative IDs:** use `urllib.parse.urljoin(base_url, id)` for **pairing only**;
  fragments resolve against the explicit capture URL, blank-node strings starting
  `_:` stay lexical. The HTML `<base>` element is ignored. Raw `@id` values remain
  in entity data, so a relative/absolute spelling change still appears at `/@id`.
  Configured fields such as `sku` are compared verbatim, without URL resolution.
  No JSON-LD expansion, compact-IRI/context resolution or blank-node equivalence.
- **Values:** object key order is ignored. Strings, nulls and number spellings
  represented as Python JSON types are compared without coercion (`1`, `1.0`,
  `true`, and `"1"` differ). Original number lexemes are not preserved (`1e0` and
  `1.0` both decode to the same float). Arrays keep order and duplicate counts;
  array diffs use positional indices, without sequence alignment. `--set-valued`
  sorts/deduplicates by canonical JSON **only at exact entity-relative pointers**.
  This opt-in treats duplicates as irrelevant; it does not apply to graph
  metadata or OpenGraph, and cannot target explicit `@list` arrays. String values
  remain lexical unless an exact `--normalize` policy opts in as described above;
  there is no automatic URL normalization.
- **OpenGraph:** consume extruct's nonuniform `namespace` + ordered `properties`
  pair list. As in upstream, only `<head><meta property="…" content="…">` in
  recognized/declared namespaces is extracted; `name="og:title"`, body metadata,
  and unknown undeclared prefixes are outside support. Compare each property's
  ordered list of strings (including repeats); order among different property
  names is ignored. Retain and compare the returned namespace map separately.
  This does not interpret structured image groups or OG URL equivalence.
- **No metadata:** two successfully parsed captures with no selected metadata
  compare equal with zero counts. This means equal *within the selected scope*,
  not that structured data exists or is valid for a search engine. A missing
  extruct result key or extraction exception is always a failure.

JSON report schema version is `1`. Each delta has `syntax`, `scope`, `entity`
(null outside entity deltas), `kind`, `path`, and whichever of `old`/`new` exist.
Absence differs from JSON null and all empty JSON values, including under string
normalization. Kinds are `added`, `removed`, `changed`,
`added_entity`, and `removed_entity`. Paths are RFC 6901 pointers relative to the
entity, graph-metadata list, OG property map, or OG namespace map indicated by
`scope`; `""` denotes a whole entity. Entity keys do not get embedded in pointers.
File digests identify the captures; source pointers and zero-based script indices
locate JSON-LD, while source lines are one-based as reported by lxml. Markdown
includes deltas/provenance and escapes untrusted HTML and table characters.

## Library and tests

```python
from qawrench.structured_data import ParityError, compare_files, make_baseline

try:
    report = compare_files("examples/structured_data/raw.html",
                           "examples/structured_data/rendered.html",
                           base_url="https://fixture.test/")
    assert report["change_count"] == 2
    # Library callers own persistence; reject coverage loss and empty entities.
    baseline_document = make_baseline(report, minimum_coverage={"json-ld.entities": 1})
except ParityError as error:
    print(error.as_dict())

# After persisting baseline_document as JSON:
# report = compare_files("reference.html", "later.html",
#                        base_url="https://fixture.test/", baseline="accepted.json")
```

Tests use the standard library unittest style, with actual extruct extraction for
normal and malformed fixture paths. No integration service or real page is needed.
Temporary fixtures stay in `.cache/` at the repository root. From a qawrench checkout:

```sh
uv run --extra test pytest tests/structured_data -q
qawrench structured-data examples/structured_data/raw.html examples/structured_data/rendered.html \
  --base-url https://fixture.test/ --out /tmp/delta.json
```

Compatibility is limited to the tested extraction versions and the documented
shapes. Runtime holds both snapshots and report in memory; large/deep documents are
not a streaming workload. No browser downloads, network requests, remote context
loading, adoption claims or performance gains are part of this integration.
Real-site behavior remains unverified. Reports and baselines keep the identifier
`"tool": "structured-data-parity"` for compatibility with existing baselines.

Original implementation: [MIT](../LICENSE). Dependencies keep their own licenses;
see [THIRD_PARTY.md](../THIRD_PARTY.md).
