# qawrench page-diff

`qawrench page-diff` (formerly `stable-page-diff`) compares two **local UTF-8 HTML
snapshots** with explicit, auditable noise rules. It is an offline CLI and library: no
monitoring server, network requests, JavaScript execution, browser, screenshots,
credentials or paid service.

Ordinary DOM content and order remain significant. Only a declared collection
of directly contained elements with unique keys is unordered. Missing required
content fails instead of returning an empty, reassuring diff.

## Install

Requires Python 3.11 or newer. BeautifulSoup and Soup Sieve are optional
dependencies of `qawrench`, installed via the `html` extra. Tested on Linux with
CPython **3.12.13**, BeautifulSoup **4.14.3** and Soup Sieve **2.8.1**.

```sh
pipx install 'qawrench[html]'    # or: pip install 'qawrench[html]'
qawrench page-diff --version     # prints the bare qawrench version

# Or run without installing:
uvx --from 'qawrench[html]' qawrench page-diff --help
```

`python -m qawrench page-diff` is equivalent to `qawrench page-diff`.
`pipx install 'qawrench[all]'` installs the optional dependencies of every command.
Without the extra, the command prints an install hint and exits 2. Installation may
download these modest Python packages; **execution is offline**.

New implementation is MIT; see [LICENSE](../LICENSE) and
[THIRD_PARTY.md](../THIRD_PARTY.md) for the dependency licenses.

## Working examples

**All `examples/page_diff/*.html` files are synthetic, hand-authored offline fixtures.**
They are not captures from any real store. The timestamp/ad changes and product
reordering below are intentionally ignored by the supplied rules.

```sh
qawrench page-diff examples/page_diff/before.html examples/page_diff/after.html \
  --rules examples/page_diff/rules.json --out /tmp/diff.json

qawrench page-diff examples/page_diff/before.html examples/page_diff/price-changed.html \
  --rules examples/page_diff/rules.json --format markdown --out /tmp/price.md

qawrench page-diff examples/page_diff/before.html examples/page_diff/after.html \
  --rules examples/page_diff/rules.json --dry-run --out /tmp/not-written.json
```

The first report has `status: "unchanged"`, `changes: []`, and six suppressed
differences: collection order, timestamp, ad subtree, heading whitespace and two
render IDs. It includes the actual old/new values for every suppression.
The price fixture reports:

```json
{
  "kind": "changed",
  "path": "/region/children/1/items/item[data-sku=\"A\"]/children/1/children/0/text",
  "old": "$10",
  "new": "$12"
}
```

Without `--out`, reports go to stdout. `--format json` is the default; Markdown
also contains old/new values, suppression evidence and the full normalization
audit. `--dry-run` still reads, validates and compares; it writes only stdout,
ignores `--out` for writing, and never executes scripts. A normal `--out` is an
explicit local report write: its parent must exist, replacement is atomic, and
input/rules files (including links to them) cannot be overwritten.

Exit codes: **0** successful comparison (changed or unchanged); **1** semantic
changes with `--fail-on-change`; **2** invalid configuration, failed input
contract, read/decode error or output error. An error produces no new report;
an existing output remains untouched. Consumers must check the exit code before
using an output that might be stale.

Library:

```python
from qawrench.page_diff import compare_files, compare_html, load_rules, ContractError

rules = load_rules("examples/page_diff/rules.json")
report = compare_files("examples/page_diff/before.html", "examples/page_diff/price-changed.html", rules)
assert report["changes"][0]["new"] == "$12"
# compare_html(before_unicode, after_unicode, rules) uses the same contract.
# ContractError means failure, never an unchanged page.
```

## Rules version 1

See [examples/page_diff/rules.json](../examples/page_diff/rules.json). Unknown properties, duplicate JSON
properties, unsupported versions, invalid selectors and incorrectly typed values
are errors. There are no implicit noise guesses.

| Field | Meaning |
| --- | --- |
| `version` | Required integer `1`. |
| `required_region` | Required Soup Sieve CSS selector matching exactly one element per snapshot; everything outside it is explicitly out of scope. |
| `required_selectors` | Optional list, default `[]`. Each must match at least one element within the region (including its root), and every match must contain ordinary non-whitespace text. |
| `required_attributes` | Optional list of `{ "selector": "a.product", "attributes": ["href", "data-sku"] }`. Each selector must match at least once within the region (including its root); every match must retain every named attribute with a nonblank value. |
| `ignore_selectors` | Optional list, default `[]`. Each entry is a selector string or `{ "selector": ".clock", "min_matches": 1, "max_matches": 1, "owner": "catalog-team", "expires": "2099-01-01" }`. Remove matching subtrees in the region. Each selector must match at least `min_matches` elements (default **1**) and at most `max_matches` if supplied, per snapshot; nested matches are removed once with their full subtree retained as evidence. |
| `normalize_whitespace` | Optional boolean, default `false`. If true, collapse Unicode whitespace runs and strip leading/trailing whitespace **per ordinary text node**; drop empty nodes. Never normalize inside `pre`, `code`, `textarea`, `script` or `style`. |
| `volatile_attributes` | Optional list of `{ "selector": ".product", "attributes": ["data-render-id"] }`, also accepting `min_matches` (default **1**), optional `max_matches`, `owner` and `expires`. Remove only those attributes from matching elements; missing attributes are permitted. Overlapping targets combine into one audit event. |
| `unordered_collections` | Optional list of `{ "selector": ".products", "items": ".product", "key_attribute": "data-sku" }`, also accepting `owner` and `expires`. Each container must match exactly once in the region. Selected items must be direct children and keys must be unique, nonempty, case-sensitive strings. |
| `sibling_identity` | Optional list of `{ "selector": "article.card", "key_attribute": "data-id" }`. Match selected siblings by this explicit attribute within each paired parent. Supports mixed content and nested matched blocks; order remains significant. No inferred keys or fuzzy matching. |

### Required text coverage

Each `required_selectors` entry adds an independent row on each side of
`rule_coverage`. For the synthetic [snapshot](../examples/page_diff/required-text-coverage/snapshot.html)
and [rules](../examples/page_diff/required-text-coverage/rules.json), identical inputs produce
`status: "unchanged"`, empty changes, suppressions and normalization audits, and:

```json
{
  "rule_id": "required_selectors[0]",
  "selector": "p",
  "matched_elements": 2,
  "matched_nodes": [{"original_path": "/region/p[0]"}, {"original_path": "/region/p[1]"}],
  "after_ignores": {
    "matched_elements": 2,
    "matched_nodes": [{"original_path": "/region/p[0]"}, {"original_path": "/region/p[1]"}]
  },
  "after_volatile": {
    "matched_elements": 2,
    "matched_nodes": [{"original_path": "/region/p[0]"}, {"original_path": "/region/p[1]"}]
  }
}
```

The top-level count and inventory describe the parsed stage. Each nested stage
describes its own selector membership after that transformation. Counts equal
inventory lengths, and every entry contains only `original_path`. Paths retain
original parsed-DOM tag positions before any ignores or normalization; each
stage follows its DOM preorder, before collection sorting. Region-root matches
use `/region`. Repeated and overlapping selectors keep separate list-position
rows, independently per side, without text values, keys or counterpart claims.
Omitted or empty `required_selectors` adds no rows. These additive fields retain
report schema version 1; whole-object consumers must accept the new rows.

Existing required-text semantics remain: every current match must contain
ordinary non-whitespace descendant text, and each stage needs at least one
match. Selectors are rerun after ignores and volatile removal. A formerly
matched element may disappear or cease matching if the remaining matches pass;
required attributes separately retain earlier obligations. Comments, attribute
values and script/style string subclasses do not substitute for ordinary text.
Ordinary text in `pre` and `code` still qualifies. Whitespace normalization,
audits, suppressions, ambiguity exclusions and semantic locations retain their
existing behavior.

```sh
qawrench page-diff examples/page_diff/required-text-coverage/snapshot.html examples/page_diff/required-text-coverage/snapshot.html \
  --rules examples/page_diff/required-text-coverage/rules.json --fail-on-change
# Exit 0; JSON includes the row above on both sides.

qawrench page-diff examples/page_diff/required-text-coverage/snapshot.html examples/page_diff/required-text-coverage/snapshot.html \
  --rules examples/page_diff/required-text-coverage/rules.json --format markdown --dry-run --out /tmp/required-text.md
# Exit 0; the Normalization rule coverage JSON block contains the same rows;
# dry-run leaves /tmp/required-text.md untouched.
```

Missing or empty required text at any validation stage still raises
`ContractError` / CLI exit 2 with empty stdout and no report. Existing output
bytes remain untouched under normal, `--dry-run` and `--fail-on-change` policies.
Required-text diagnostics identify the side, configured list-position rule ID,
selector and stage (`parsed`, `after_ignores` or `after_volatile`). For example,
`<main><p>Keep</p><p> </p></main>` with `required_selectors: ["main", "p", "p"]`
fails with:

```text
before: required text contract failed: required_selectors[1] selector='p' stage=parsed empty text original_path='/region/p[1]'
```

Within each stage, configured rule order wins, then the first empty match in
that stage's DOM preorder. Paths retain original parsed positions, including
siblings subsequently ignored. When no element matches, the diagnostic instead
ends with `expected at least 1 matched elements, got 0 original_path=null
original_scope_path='/region'`. It does not attribute failure to a former match.
For example, requiring `p[title]` and then removing the sole matching `title`
with a volatile rule fails at `after_volatile` with that null element path.
Region-root matches use `/region`. The separate region-wide empty-text gate
still runs first; its error identifies the stage and `original_path='/region'`
without inventing a configured text-rule ID. Before-side validation completes
before after-side validation, and required-attribute/key gate precedence stays
the same. These diagnostics contain configured selectors and structural paths,
never observed text or attribute values. Successful JSON/Markdown bytes and
coverage inventories are unchanged by the diagnostic enhancement.

Required-attribute, identity and collection/key failures keep those same output
guarantees. Required text rows follow existing selector and required-attribute
rows, before enabled whitespace coverage. Markdown exposes the same data in
its existing coverage JSON block.

### Whitespace coverage inventory

With `normalize_whitespace: true`, each side's coverage row includes
`matched_nodes` containing only `original_path`, one per eligible surviving text
node. For the synthetic [snapshot](../examples/page_diff/whitespace-coverage/snapshot.html)
and [rules](../examples/page_diff/whitespace-coverage/rules.json), identical inputs produce
`status: "unchanged"`, no semantic findings or suppressions, and this row on
both sides:

```json
{
  "rule_id": "normalize_whitespace",
  "text_nodes": 3,
  "changed_text_nodes": 2,
  "matched_nodes": [
    {"original_path": "/region/#text[0]"},
    {"original_path": "/region/p[1]/#text[0]"},
    {"original_path": "/region/p[2]/#text[0]"}
  ]
}
```

The first node normalizes to empty, the second stays `Keep`, and the third
normalizes to `A B`. All three belong in the inventory; only two count as
changed observations. The ignored `p[0]` still contributes to original indices.
Only ordinary `NavigableString` nodes that the existing whitespace rule observes
are included. Ignored subtrees, comments and other string subclasses, and text
with any `pre`, `code`, `textarea`, `script` or `style` ancestor are excluded.
Inventories follow original DOM preorder, including collection separators,
before keyed sorting; they are independent per snapshot. They contain no text
values, inferred identities or counterpart claims. Paths describe the original
parsed DOM relative to the region, not source offsets or CSS selectors.

Inventory length equals `text_nodes`; `changed_text_nodes` counts observations
whose original and normalized values differ, rather than suppressed differences.
Zero eligible nodes produce `matched_nodes: []` (for example, a region containing
only protected `code` text). Disabled normalization adds no whitespace row.
Existing audit values/order, suppressions, semantic locations and other coverage
inventories retain their meanings. This is additive under report schema version 1.

```sh
qawrench page-diff examples/page_diff/whitespace-coverage/snapshot.html examples/page_diff/whitespace-coverage/snapshot.html \
  --rules examples/page_diff/whitespace-coverage/rules.json --fail-on-change
# Exit 0; both sides contain the row above.

qawrench page-diff examples/page_diff/whitespace-coverage/snapshot.html examples/page_diff/whitespace-coverage/snapshot.html \
  --rules examples/page_diff/whitespace-coverage/rules.json --format markdown --dry-run --out /tmp/whitespace.md
# Exit 0; stdout Markdown has the same row in Normalization rule coverage;
# dry-run leaves /tmp/whitespace.md untouched.
```

Malformed rules (including a nonboolean `normalize_whitespace`), missing required
content/attributes, forbidden key removal and invalid collection keys still fail
with `ContractError` / CLI exit 2, empty stdout and no new report. Existing output
bytes remain untouched, also with `--dry-run` or `--fail-on-change`. Ordinary text,
protected text and comment edits remain semantic findings; `--fail-on-change`
returns 1 when findings remain.

### Required attributes

Use `required_attributes` when retained text alone cannot establish a valid
snapshot, such as a product link that lost its `href` or machine-readable SKU.
Each entry accepts **only** `selector` and `attributes`. Attribute names must be
a nonempty list of unique lowercase HTML names using
`[a-z_:][a-z0-9_.:-]*`; invalid types, duplicate names and unknown keys fail.
Omitting this field preserves existing report bytes and behavior; explicit `[]`
is accepted and retained in effective rules without adding coverage rows.

Every match is checked independently on the parsed DOM, after ignored subtrees
are removed, and after volatile attributes are removed. The selector is rerun
at each stage, and every earlier matched element remains required even if it
stops matching the selector. Removing one required descendant fails even if
other matches survive. Newly matching elements are checked too. A required
attribute selected as volatile therefore fails after removal. Zero matches at
any stage fail. A value must contain a character remaining after Python's
Unicode whitespace `str.strip()` test; missing and empty/whitespace-only values
fail separately. Values are **never rewritten**: surrounding whitespace,
Unicode spelling and ordinary attribute edits still affect the semantic diff.
This does not validate URLs, fetch links or render HTML.

Successful JSON and Markdown reports include one `rule_coverage.before` / `.after`
row per configured entry. For the synthetic
[product link](../examples/page_diff/required-attributes/before.html), each side has:

```json
{
  "rule_id": "required_attributes[0]",
  "selector": "a.product",
  "attributes": ["href", "data-sku"],
  "matched_elements": 1,
  "matched_nodes": [{"original_path": "/region/a[0]"}],
  "after_ignores": {
    "matched_elements": 1,
    "matched_nodes": [{"original_path": "/region/a[0]"}]
  },
  "after_volatile": {
    "matched_elements": 1,
    "matched_nodes": [{"original_path": "/region/a[0]"}]
  }
}
```

The top-level count/inventory describes initial matches; the two stage objects
describe their own matches. Inventories follow parsed-DOM preorder, independently
per rule and side, and contain paths captured **before** all normalization.
They do not pair elements across snapshots or expose attribute values. Existing
normalization and suppression evidence is unchanged; these gates add no noise
rules. Diagnostics identify the side, rule, selector, stage, attribute and
original element path without printing observed attribute values. A zero-match
error lists the required attributes and reports `original_path=null` with
`original_scope_path='/region'`, since no matched element has a path.

```sh
qawrench page-diff examples/page_diff/required-attributes/before.html examples/page_diff/required-attributes/changed.html \
  --rules examples/page_diff/required-attributes/rules.json --fail-on-change
# Exit 1: ordinary href change /one -> /two, with coverage on both sides.

qawrench page-diff examples/page_diff/required-attributes/before.html examples/page_diff/required-attributes/missing.html \
  --rules examples/page_diff/required-attributes/rules.json --out /tmp/required.json
# Exit 2: after, required_attributes[0], href missing, /region/a[0]; no new report.
# blank.html also exits 2: href contains only Unicode whitespace.
```

Failed gates raise `ContractError` / CLI exit 2 with empty stdout, leave existing
output intact and create no new report, including with `--dry-run` or
`--fail-on-change`. Required attributes complement the existing required text,
identity and strict collection contracts; they do not replace them.

### Coverage gates and rule maintenance

**Behavior change:** ignore and volatile selectors now require at least one match
in **each** snapshot, including legacy string ignores. A selector that stopped
matching, or is stale on both snapshots, fails with `ContractError` (CLI exit 2).
For an intentionally optional rule, use `min_matches: 0` explicitly. This accepts
absence on either side and still reports zero-match coverage. Any nonnegative
integer minimum is accepted; booleans are rejected. Collection containers retain
their exactly-one requirement and do not accept `min_matches` or `max_matches`.

Ignore and volatile entries also accept optional **inclusive** `max_matches`.
It must be a nonnegative integer at least the effective `min_matches` (default 1);
booleans, null, strings and fractional values are errors. For a rule that must
match nothing, set both `min_matches: 0` and `max_matches: 0`. Omitting the maximum
leaves the rule unbounded and does not add a `max_matches` field to its coverage
row. Legacy string ignores remain unbounded; use an object to set a maximum.
Each bound applies independently per rule and snapshot, including identical
snapshots. Ignore counts include nested matches before any removal. Volatile
counts include every matched element after ignores, even if none of the named
attributes is present, before any volatile attributes are removed. Overlapping
rules do not share a match budget. Configured maxima appear in effective rules
and JSON/Markdown coverage. An excess fails with `ContractError` / exit 2,
without writing a report, including with `--dry-run` or `--fail-on-change`.

The synthetic [bound fixtures](../examples/page_diff/bounds/rules.json) keep a product
`data-price` edit from `$10` to `$12` while suppressing two declared noise edits:

```sh
qawrench page-diff examples/page_diff/bounds/before.html examples/page_diff/bounds/after.html \
  --rules examples/page_diff/bounds/rules.json --fail-on-change
# Expected exit 1: one $10 -> $12 change, two suppressions; both rules match 1.

qawrench page-diff examples/page_diff/bounds/before.html examples/page_diff/bounds/after.html \
  --rules examples/page_diff/bounds/broadened-ignore-rules.json

qawrench page-diff examples/page_diff/bounds/before.html examples/page_diff/bounds/after.html \
  --rules examples/page_diff/bounds/broadened-volatile-rules.json
# Each broadened rule matches the product too: exit 2, no report.
```

Bound diagnostics retain side, list-position rule ID, selector, inclusive bound,
actual count and owner (or `(unspecified)`). They add the actual selection stage:
`parsed` for ignores and `after_ignores` for volatile rules. For example,
`<main><h1>Keep</h1><p>A</p><p>B</p></main>` with an ignore rule
`{"selector": "p", "max_matches": 1, "owner": "catalog-team"}` fails with:

```text
qawrench page-diff: before: normalization coverage failed: ignore_selectors[0] selector='p' stage=parsed expected at most 1 matched elements, got 2 (owner='catalog-team') original_paths=["/region/p[0]", "/region/p[1]"]
```

`original_paths` is a JSON array of every actual match in that stage's DOM
preorder, using original parsed-DOM positions, before collection sorting.
Ignored same-tag siblings retain their indices; region-root matches use
`/region`. Repeated/overlapping rules keep their own membership and bounds.
Volatile selectors are evaluated after all ignores and before any volatile
attribute removal, even when a selected element has no named attribute.
A zero-match minimum failure instead ends with
`original_path=null original_scope_path='/region'`; it never lists former
members. Optional zero matches remain successful when `min_matches: 0` permits
them. Diagnostics print configured selectors/owners and structural paths, never
observed text or attribute values. Before-side validation still completes first;
existing gate, stage and configured-rule precedence are unchanged. A bound can
fail on a region-root ignore before the separate forbidden-removal gate runs.

The broadened examples above identify `.clock, .product` at `stage=parsed`
and `.render, .product` at `stage=after_ignores`, respectively, with the original
paths of both selected elements. Removing their maxima would incorrectly
suppress the price edit and report unchanged. Successful JSON/Markdown report
bytes and schema are unchanged by this diagnostic enhancement. Failures produce
empty stdout, no new report and byte-identical existing output, including with
`--dry-run` or `--fail-on-change` and either output format.

Bounds only constrain counts: a wrong selector matching the same allowed number
of elements can still hide content, so retain suppression evidence for review.

Each `ignore_selectors` coverage row includes `matched_nodes`, even for identical
snapshots. For the synthetic [nested snapshot](../examples/page_diff/ignore-coverage/snapshot.html)
and rule `.noise`, each side has:

```json
{
  "rule_id": "ignore_selectors[0]",
  "selector": ".noise",
  "matched_elements": 2,
  "min_matches": 1,
  "matched_nodes": [
    {"original_path": "/region/aside[0]"},
    {"original_path": "/region/aside[0]/em[0]"}
  ]
}
```

Each inventory contains every element directly matched by that rule, in original
parsed-DOM preorder before any removal. Its length equals `matched_elements`;
nested matches count independently, overlapping/repeated rules have independent
inventories in configured rule order, and optional zero matches report `[]`.
Each snapshot supplies its own membership and original same-tag sibling indices.
These paths assert no cross-snapshot pairing, inferred identity or source offsets.
An entry contains only `original_path`; directly matching rule descriptors remain
in the existing audit. One outermost-root observation/removal still retains the
full subtree and nested audit matches. Identical snapshots have no suppressed
differences, while their coverage and audit still expose these observations.

```sh
qawrench page-diff examples/page_diff/ignore-coverage/snapshot.html examples/page_diff/ignore-coverage/snapshot.html \
  --rules examples/page_diff/ignore-coverage/rules.json --fail-on-change
# Exit 0: unchanged, two located matches per side, one subtree audit per side.
# Add --format markdown for the same inventory in the coverage JSON block.
```

This field is additive under report schema version 1. Markdown retains its
existing HTML, backtick and Unicode escaping. Minimum/maximum coverage failures
still raise `ContractError` / CLI exit 2 with no report or stdout and no output
mutation, including with `--dry-run` or `--fail-on-change`. Collection inventories,
sibling identities, required attributes and suppression evidence retain their
contracts.

Each `volatile_attributes` coverage row also includes `matched_nodes`, even for
identical snapshots and targets with missing attributes. With the synthetic
[post-ignore snapshot](../examples/page_diff/volatile-coverage/snapshot.html) and
[rules](../examples/page_diff/volatile-coverage/rules.json), each side's volatile row is:

```json
{
  "rule_id": "volatile_attributes[0]",
  "selector": "p",
  "matched_elements": 2,
  "min_matches": 1,
  "attributes_present": {"data-render": 1},
  "matched_nodes": [
    {"original_path": "/region/p[1]"},
    {"original_path": "/region/p[2]"}
  ]
}
```

The ignored `p[0]` is absent from volatile membership. The surviving missing and
empty attributes both count as matched elements; only the empty attribute counts
as present. Inventory length equals `matched_elements`, with one entry per
selected element regardless of how many attributes the rule names. Selectors run
after ignores and before any volatile removal. Inventories retain that DOM
preorder and the original pre-removal same-tag indices, including `/region` if
the region root matches. Repeated/overlapping rules retain independent inventories
in configured order; optional zero matches have `[]`. Each entry contains only
`original_path`, with no attribute values, inferred identity or counterpart claim.
The snapshot-local inventories do not change combined per-element/attribute audit
observations, `original_target_path`, suppression evidence, ambiguity exclusions,
ignore/identity/collection inventories or required-attribute gates.

```sh
qawrench page-diff examples/page_diff/volatile-coverage/snapshot.html examples/page_diff/volatile-coverage/snapshot.html \
  --rules examples/page_diff/volatile-coverage/rules.json --fail-on-change
# Exit 0: unchanged, no suppressions, two located volatile targets per side.
# Add --format markdown for the same inventory in the coverage JSON block.
```

This is additive under report schema version 1; whole-object consumers must accept
the new field. Markdown preserves its existing HTML, backtick and Unicode escaping.
Minimum/maximum failures remain `ContractError` / CLI exit 2 with empty stdout,
no new report and unchanged existing output, including with `--dry-run` and
`--fail-on-change`. Bounds count selected elements, including missing attributes.

Optional `owner` is a nonblank string identifying who maintains a rule; it appears
in coverage and gate diagnostics. Optional `expires` must be a real, canonical
`YYYY-MM-DD` date. The rule fails **at the start of that UTC date**, even when it
is optional and matches nothing. The current UTC date is captured once per
comparison and recorded as `rule_coverage.evaluated_on`. Loading/validating rules
checks date syntax; comparing enforces expiry before normalization. There is no
CLI bypass. Rules without expiry do not expire automatically.

The synthetic [coverage fixtures](../examples/page_diff/coverage/rules.json) demonstrate both
success and failure with the installed command:

```sh
qawrench page-diff examples/page_diff/coverage/before.html examples/page_diff/coverage/after.html \
  --rules examples/page_diff/coverage/rules.json --out /tmp/coverage.json

# Expected exit 2; no new report, existing --out stays untouched.
qawrench page-diff examples/page_diff/coverage/before.html examples/page_diff/coverage/stale-selector.html \
  --rules examples/page_diff/coverage/rules.json --out /tmp/stale.json

# Expected exit 2 naming the expired rule and its owner.
qawrench page-diff examples/page_diff/coverage/before.html examples/page_diff/coverage/after.html \
  --rules examples/page_diff/coverage/expired-rules.json
```

The success has `status: "unchanged"`, two suppressions, `.clock` coverage of 1
on both sides, and explicit `.optional-ad` coverage of 0. Renaming `.clock` to
`.time` produces a diagnostic containing
`after: normalization coverage failed: ignore_selectors[0] selector='.clock' expected at least 1 matched elements, got 0`.
An unmet gate fails regardless of `--fail-on-change` or `--dry-run`.

Collection containers may contain only declared items and whitespace separators;
put headings outside them. Zero items is valid if the required region still has
text (e.g. its heading). Non-item elements, comments, stray text, nested or
overlapping collections, and items selected below direct children fail. These
restrictions keep unkeyed content from being silently lost or reordered.
Each collection is keyed independently; movement between collections is a
removal/addition. Keys are never guessed from text, normalized, or made volatile.
Ignores cannot remove a required region, a collection or a keyed item.
Required selectors are rechecked after ignores and volatile-attribute removal.

Each `unordered_collections` coverage row includes `original_path` for its
container and `matched_items`, even when both snapshots are identical. For the
synthetic [two-item snapshot](../examples/page_diff/collection-coverage/snapshot.html), each
side reports:

```json
{
  "rule_id": "unordered_collections[0]",
  "selector": ".items",
  "matched_elements": 1,
  "min_matches": 1,
  "item_count": 2,
  "original_path": "/region/section[0]",
  "matched_items": [
    {"original_path": "/region/section[0]/article[0]", "original_parent_path": "/region/section[0]", "key": "Z"},
    {"original_path": "/region/section[0]/article[1]", "original_parent_path": "/region/section[0]", "key": "A"}
  ]
}
```

The inventory follows original parsed-DOM item order, before ignores or collection
sorting, and its length equals `item_count`. Empty collections report `[]`.
A collection at the region root has container path `/region`. Keys retain their
exact parsed attribute strings, including decoded HTML entities and surrounding
nonblank whitespace. Missing, blank or duplicate keys still fail before any
report is emitted. An ignored same-tag sibling outside the container still
counts toward original positions; non-item children inside it remain invalid.
Each rule and snapshot has its own inventory: repeated keys in different
containers are independent, and entries do not assert cross-snapshot pairings.
Paths describe the original parsed DOM, not source offsets or CSS selectors.

```sh
qawrench page-diff examples/page_diff/collection-coverage/snapshot.html examples/page_diff/collection-coverage/snapshot.html \
  --rules examples/page_diff/collection-coverage/rules.json --fail-on-change
# Exit 0: unchanged, two located items per side, no suppressed differences.
# Add --format markdown for the same inventory in the coverage JSON block.
```

These fields are additive under report schema version 1; consumers comparing
whole coverage objects must accept them. Markdown uses its existing HTML,
backtick and Unicode escaping. Collection key-list audits and suppressions,
sibling identity inventories and semantic findings retain their contracts.

The region must contain ordinary non-whitespace text; whitespace-only,
comment-only, script-only and image-only regions are unsupported and fail.
HTML attribute names in rules must be lowercase. A vanished container, missing
key, duplicate key, duplicate HTML attribute or non-UTF-8 file is an error.
BeautifulSoup's tolerant HTML parser does not certify well-formed HTML: the parse
contract is these explicit structural/content checks on its resulting DOM.

## Matching reordered sibling blocks

Use `sibling_identity` when ordered blocks have stable IDs but their parent also
contains headings, comments or other ordinary content. It identifies elements;
it does not declare their order irrelevant. The synthetic
[identity fixtures](../examples/page_diff/identity/rules.json) change
`[A: title=old, $10; heading; B: $20]` to
`[B: $20; heading; A: title=new, $12]`:

```sh
qawrench page-diff examples/page_diff/identity/before.html examples/page_diff/identity/after.html \
  --rules examples/page_diff/identity/rules.json --fail-on-change --out /tmp/identity.json
# Expected exit 1: two changed findings (title and price), two moved findings.

qawrench page-diff examples/page_diff/identity/before.html examples/page_diff/identity/ambiguous.html \
  --rules examples/page_diff/identity/rules.json --fail-on-change
# Expected exit 1: explicit duplicate_key ambiguity for A, plus removal of B.
```

The price finding is:

```json
{
  "kind": "changed",
  "path": "/region/children/sibling[sibling_identity[0]:data-id=\"A\"]/children/0/children/0/text",
  "old": "$10",
  "new": "$12",
  "before_path": "/region/article[0]/p[0]/#text[0]",
  "after_path": "/region/article[1]/p[0]/#text[0]"
}
```

Matching is scoped to **one rule and one paired parent**, using exact,
case-sensitive, nonempty attribute strings. IDs may repeat under different
parents or different rules. Changing a key or moving between parents yields
remove/add. There is no automatic parent identity: select a stable parent too
if it also needs matching across a reorder. Unselected siblings retain sequence
alignment. A `moved` finding records each matched element whose relative position
among surviving paired siblings changes, with old/new normalized child indices
and original paths. A swap can report two moves; this is not a minimal edit
script. Inserting/removing a peer alone does not move all later elements.

A missing/blank key emits `kind: "ambiguous", reason: "missing_key"`; a duplicate
on either side emits `reason: "duplicate_key"`. Its `identity` names the rule,
attribute and key. Its `old`/`new` arrays retain **every candidate's original path
and full normalized subtree**, without pairing their text/attributes by position.
The finding's `before_path`/`after_path` locate the containing parents. Ambiguity
counts as `status: "changed"` even for identical snapshots and makes
`--fail-on-change` exit 1; it is a reviewable report, not an input error. Duplicate
HTML attributes and duplicate keys in strict unordered collections still fail
with exit 2. Subtrees added/removed as a whole retain their contents without
attempting descendant matching.

Identity selectors run before normalization and may match zero elements (visible
in `rule_coverage`); use `required_selectors` if their presence is required. These
entries accept only `selector` and `key_attribute`. Selecting the required-region
root, overlapping identity rules, also selecting a strict unordered item,
ignoring a selected identity element/ancestor, or making its key volatile fails
with `ContractError` (CLI exit 2, no new report). Ignores inside a selected block
and normalization of its other attributes/text are supported.

Each `sibling_identity` row in `rule_coverage.before` and `.after` includes
`matched_nodes`, even for unchanged snapshots without normalization observations.
For the synthetic [two-card snapshot](../examples/page_diff/identity-coverage/snapshot.html),
each side reports:

```json
{
  "rule_id": "sibling_identity[0]",
  "selector": "article",
  "matched_elements": 2,
  "key_attribute": "data-id",
  "matched_nodes": [
    {"original_path": "/region/article[0]", "original_parent_path": "/region", "key": "A"},
    {"original_path": "/region/article[1]", "original_parent_path": "/region", "key": "B"}
  ]
}
```

The inventory contains every selected candidate in original parsed-DOM preorder;
its length equals `matched_elements`, and zero matches produce `[]`. Element and
parent positions precede all normalization, including ignore removal and collection
sorting. `key` retains the exact parsed attribute string (including surrounding
whitespace in a nonblank key); missing, empty or whitespace-only keys are `null`.
Duplicate and missing-key candidates remain individually listed. Rule and key
attribute come from the enclosing coverage row. Repeated keys under different
parents or rules remain independent. These are per-snapshot selector observations:
the inventory makes no successful-identity or cross-snapshot pairing claim and
does not change ambiguity findings. Paths are relative to the parsed required
region, not source offsets or browser selectors.

```sh
qawrench page-diff examples/page_diff/identity-coverage/snapshot.html examples/page_diff/identity-coverage/snapshot.html \
  --rules examples/page_diff/identity-coverage/rules.json --fail-on-change
# Exit 0: unchanged, no findings/suppressions/audit events; two located candidates per side.
# Add --format markdown for the same inventory in the Normalization rule coverage JSON block.
```

This additive field retains report `schema_version: 1`; consumers comparing
complete coverage objects must accept it. Markdown uses its existing HTML- and
backtick-safe JSON escaping, preserving the decoded keys and paths.

Normalization audit paths inside selected blocks use identity segments, so
suppression evidence follows the same block through a reorder. Every observation
records its positional `original_path`; with sibling identity enabled it also
records an `ambiguous_identity` flag. Beneath an ambiguous identity on either
side, observations remain in the audit but are not paired into `suppressed`
claims, even when the ambiguous side has no normalization observations.

The synthetic [suppression-location fixtures](../examples/page_diff/suppression-paths/rules.json)
reorder A/B cards after an ignored same-tag sibling. They retain a `$10` to `$12`
price edit and two moves, while suppressing two clock edits, two render attributes
and one whitespace difference:

```sh
qawrench page-diff examples/page_diff/suppression-paths/before.html examples/page_diff/suppression-paths/after.html \
  --rules examples/page_diff/suppression-paths/rules.json --fail-on-change
# Expected exit 1: three semantic findings and five suppressions.
# Add --format markdown to show both locations beneath each suppression.
```

The suppressed `data-render` change from `a` to `d` has
`before_path: "/region/article[1]/@data-render"` and
`after_path: "/region/article[2]/@data-render"`. Consumers can locate each
observation directly without looking it up in the full audit.

The [suppression-rule fixtures](../examples/page_diff/suppression-rules/rules.json) also
exercise overlapping rules, repeated selectors and changing selector membership
inside reordered cards:

```sh
qawrench page-diff examples/page_diff/suppression-rules/before.html examples/page_diff/suppression-rules/after.html \
  --rules examples/page_diff/suppression-rules/rules.json --fail-on-change
# Expected exit 1: $10 -> $12 and two moves; four suppressed differences.
# Add --format markdown to show locations, rules and per-side matched nodes.
```

The `old-a` to `new-a` attribute suppression has:

```json
{
  "before_rules": [
    {"rule_id": "volatile_attributes[0]", "selector": "article"},
    {"rule_id": "volatile_attributes[1]", "selector": "article"},
    {"rule_id": "volatile_attributes[2]", "selector": "[data-render^=old]"}
  ],
  "after_rules": [
    {"rule_id": "volatile_attributes[0]", "selector": "article"},
    {"rule_id": "volatile_attributes[1]", "selector": "article"},
    {"rule_id": "volatile_attributes[3]", "selector": "[data-render^=new]"}
  ]
}
```

Each outermost ignore observation in the full normalization audit also lists
`matched_nodes`: one entry per directly or nested matched element, in original
parsed-DOM preorder (root first). For card A before the reorder, it includes:

```json
[
  {
    "original_path": "/region/article[0]/aside[0]",
    "rules": [
      {"rule_id": "ignore_selectors[0]", "selector": ".noise"},
      {"rule_id": "ignore_selectors[1]", "selector": ".noise"},
      {"rule_id": "ignore_selectors[2]", "selector": ".old-noise"}
    ]
  },
  {
    "original_path": "/region/article[0]/aside[0]/em[0]",
    "rules": [{"rule_id": "ignore_selectors[4]", "selector": ".inner"}]
  }
]
```

After the reorder, those paths start with `/region/article[1]` and the root's
last descriptor is `ignore_selectors[3]` / `.new-noise`. The corresponding ignore
suppression exposes these same arrays as `before_matched_nodes` and
`after_matched_nodes`, so consumers need no audit lookup. Card B's one-sided
suppression has `before_matched_nodes: []` and an `after_matched_nodes` array
containing its root at `/region/article[0]/aside[0]`. Markdown shows both arrays
under **Suppressed differences**, as `before matched nodes` and `after matched
nodes`, as well as retaining the full audit. Repeated selectors retain all list-position IDs;
rules within a node stay in configuration order even when descendant rules
precede root rules. Unmatched descendants have no entry. The full subtree is
still removed once, with one observation and at most one suppression per root.

The [volatile-target fixtures](../examples/page_diff/suppression-targets/rules.json) distinguish
matched elements with missing or empty attributes from unobserved targets, while
retaining a price edit through a card reorder and an ignored same-tag sibling:

```sh
qawrench page-diff examples/page_diff/suppression-targets/before.html examples/page_diff/suppression-targets/after.html \
  --rules examples/page_diff/suppression-targets/rules.json --fail-on-change
# Expected exit 1: $10 -> $12 and two moves; three suppressed differences.
# Add --format markdown to show before/after target locations.
```

Card A's missing `data-render` becomes `new-a`. Its suppression includes:

```json
{
  "old": null,
  "new": "new-a",
  "before_path": null,
  "after_path": "/region/article[2]/@data-render",
  "before_target_path": "/region/article[1]",
  "after_target_path": "/region/article[2]"
}
```

Card B starts with an empty attribute: its `before_path` is
`/region/article[2]/@data-render`, and its `before_target_path` is
`/region/article[2]`. If a volatile selector matches only one snapshot, the
unobserved side's target path is `null`, even if an unselected element or
attribute exists there. Target paths locate observations independently; they
do not assert a DOM counterpart for a side with no observation.

The [audit-target fixtures](../examples/page_diff/audit-targets/rules.json) compare an identical
snapshot containing missing, empty and present volatile attributes. There are
no suppressed differences, but every volatile observation retains its matched
element's `original_target_path` in the full audit, including in Markdown:

```sh
qawrench page-diff examples/page_diff/audit-targets/snapshot.html examples/page_diff/audit-targets/snapshot.html \
  --rules examples/page_diff/audit-targets/rules.json --fail-on-change
# Expected exit 0: unchanged, no suppressions, three volatile observations per side.
# Add --format markdown to see original_target_path in the Full normalization audit.
```

For card A, the observation includes these fields independently on each side:

```json
{
  "rule": "volatile_attributes",
  "old": null,
  "new": null,
  "original_path": "/region/article[1]/@data-render",
  "original_target_path": "/region/article[1]"
}
```

`original_path` remains the attribute-address path even when that attribute is
missing. The target path locates the matched element before ignores or other
normalization; the ignored banner still contributes to the original index.

## What the report means

`schema_version: 1` reports `status`, effective `rules`, `scope`, `changes`,
`suppressed`, `normalizations`, `rule_coverage` and (for file comparisons) `inputs`.

- `changes` contains `added`, `removed` and `changed` findings with old/new
  values, plus opt-in `moved` and `ambiguous` identity findings described above.
  Added/removed items include their full normalized subtree, including text. Ordered sequences use deterministic sequence alignment; collection
  maps align by exact key. Ordinary attribute changes and comments are retained.
- `suppressed` names the specific rule and audit path (with configured identity
  segments where applicable), both original values, their common normalized value,
  and independent `before_path` / `after_path` locations. These locations are
  recorded before normalization, including ignored subtrees. An absent observation
  has a `null` location. A missing volatile attribute also has a `null` location;
  an existing empty-string attribute retains its path. If a selector matches only
  one side, the unobserved side is `null` even if it has an unselected attribute.
  Its semantic changes are still reported. Pure reordered collections include
  both key orders. A collection addition/removal is not claimed as a suppressed
  reorder. All applicable transformations remain in the detailed audit.
- Each suppression also has `before_rules` and `after_rules`: arrays of
  `{ "rule_id": "ignore_selectors[0]", "selector": ".clock" }` descriptors
  in configured rule order. IDs refer to effective rule list positions and
  coverage rows; repeated selectors retain their distinct IDs. Overlapping
  rules still produce one suppression per target. The unobserved side has `[]`.
  A matched volatile target with a missing attribute retains its rules even
  though its attribute location is `null`. Only rules directly matching an
  outermost ignored root contribute to that observation's `rules` array;
  descendant-only ignore matches are individually located in audit `matched_nodes`
  and counted in coverage. Collection rules
  use `unordered_collections[index]`; whitespace uses
  `{ "rule_id": "normalize_whitespace", "selector": null }`. Identity rules
  pair observations and are not normalization provenance. Markdown shows both
  rule arrays beneath each suppression, independently of the full audit.
- Ignore suppressions also expose `before_matched_nodes` and
  `after_matched_nodes`: the corresponding audit observation's `matched_nodes`
  array, or `[]` for an absent observation. Each array independently retains
  original paths and directly matching rules in parsed-DOM preorder, root first.
  Their lengths can differ; entries are evidence, not cross-snapshot descendant
  pairings. `before_rules` / `after_rules` remain root-only. Unchanged ignored
  values and ambiguous identities do not gain suppression claims. Other
  normalization types have no matched-node suppression fields. Markdown shows
  both arrays beneath each ignore suppression using the same JSON escaping as
  other values.
- Volatile-attribute suppressions also expose `before_target_path` and
  `after_target_path`: original matched-element locations, including when the
  attribute is missing or empty. An absent observation has `null`. Existing
  `before_path` / `after_path` continue to locate actual attributes and use
  `null` for missing attributes. Rules, values, audit and suppression counts
  retain their existing meanings; other normalization types and semantic
  findings have no target-path fields. Markdown labels these values `before
  target location` and `after target location`, beneath each volatile suppression.
  Ambiguous identities remain audited without suppression claims.
- `normalizations.before` and `.after` contain every rule observation and its
  old/new value, **including unchanged observations**. They explain partial
  normalization even when text still changes substantively. Audit paths use
  original DOM tag/type indices and keyed item/sibling identity segments; semantic paths refer
  to the resulting normalized representation, so the two path forms differ.
  Every observation includes its own `rules` descriptor array using the same
  contract, including observations under ambiguous identities. Existing
  `selectors` / `selector` audit fields remain available. Ignore observations
  additionally include `matched_nodes` with each matched element's original path
  and directly matching rules, including nested matches. The root's entry uses
  the observation's `original_path` and `rules`. An absent observation has no
  audit entry; ambiguity retains individually located matches without pairing
  them into suppression claims. Other normalization types have no `matched_nodes`.
  Every volatile-attribute observation additionally includes
  `original_target_path`: its original matched-element path, including missing,
  empty, unchanged and ambiguous observations. Its existing `original_path`
  still addresses the attribute, including when missing. An unobserved target
  has no audit entry; no counterpart is inferred. Other normalization types
  have no `original_target_path`. Markdown includes this field in the full
  audit JSON block with the same escaping as other values.
  These report fields
  are additive; rules and report schema remain version 1.
- `rule_coverage.before` and `.after` contain one row per configured selector rule,
  identified by its list position (`ignore_selectors[0]`, etc.), even when rules
  overlap, match unchanged values or explicitly allow zero matches. Rows include
  `selector` and `matched_elements`. Ignore/volatile rows also include
  `min_matches` and configured
  `max_matches`/`owner`/`expires`.
  Ignore matches are counted before any subtree removal, including nested
  matches; `matched_nodes` locates every direct match per rule in original preorder.
  Volatile matches are counted after ignores but before any attributes are removed;
  `matched_nodes` locates every selected surviving element in DOM preorder using
  original positions, including missing attributes. `attributes_present` counts
  actual occurrences per attribute.
  Collection matches/items are counted before ignores; `item_count` can be zero.
  Their `original_path` locates the container and `matched_items` locates every
  original item and parent with its exact key, in original item order.
  Identity rules add pre-normalization match counts and `key_attribute`, with
  zero matches allowed. Enabled whitespace normalization adds a row with eligible
  surviving `text_nodes` and `changed_text_nodes`, excluding protected text. Its
  `matched_nodes` inventories original text paths in DOM preorder, including
  unchanged and normalized-empty nodes; zero eligible nodes produce `[]`. Counts describe
  rule coverage, not numbers of suppressed differences. Markdown includes this
  same coverage data. Required-text rows have parsed counts/inventories plus
  `after_ignores` and `after_volatile` counts/inventories as described above;
  they have no minimum/maximum metadata. A failed gate emits an error, not a
  partial success report.

Paths start at `/region`; key segments contain JSON-quoted values. Every path
segment escapes `~` as `~0` and `/` as `~1` (JSON Pointer escaping). These paths
are explanatory identifiers, not CSS selectors. There is no guaranteed stable
identity for ordinary unkeyed siblings when they are inserted or removed.
Every semantic and suppressed finding includes `before_path` and `after_path`, independently
located in the original parsed DOM **before** ignores, whitespace removal or
collection sorting. A nonexistent side is `null`. Tag/type indices are zero-based
among siblings of the same tag/type; text paths end in `#text[index]` and
attribute paths in `/@name`. Structural fields such as `name` retain a field
suffix. These are exact parsed-DOM locations relative to the selected region,
not HTML source byte offsets, line/column spans or browser selectors. Semantic
`path` continues to refer to the normalized representation. Markdown includes
both original locations, identity metadata and ambiguity reasons.

This is a parsed-DOM comparison: tag/attribute case normalization, entity spelling,
attribute quoting/order and source syntax are outside the comparison contract.
Whitespace-only text is preserved at parse time, as are class attribute strings;
only the opt-in rule removes their differences. Opt-in whitespace trimming can
hide intentional inline spacing, so leave it disabled for whitespace-sensitive
content. Ignored/volatile values are deliberately absent from semantic findings
and are still present in report evidence. Reports can contain the supplied text.
Text-node boundaries remain meaningful; this is not a rendered-text equivalence
engine. Recursive trees/diffs are intended for modest saved snapshots, not huge
or adversarial HTML; excessive nesting fails rather than returning unchanged.

## Tests and measured comparison

From a qawrench checkout:

```sh
uv run --extra test pytest tests/page_diff -q
qawrench page-diff examples/page_diff/before.html examples/page_diff/after.html \
  --rules examples/page_diff/rules.json --out /tmp/diff.json
python examples/page_diff/experiment.py
```

Tests run sequentially. They cover real text/price/attribute changes, exact keyed
add/remove including empty collections, order preservation outside declared
collections, whitespace and protected text, nested ignores, suppression values,
malformed rules/selectors, missing/blank content, duplicate keys/attributes,
non-UTF-8 files, safe output, CLI exit codes, dry-run and the full labeled fixture
comparison, per-rule coverage, selector drift, explicit optional rules, owner
metadata and UTC expiry boundaries. They also cover selector bounds (inclusive
limits, zero maxima, excess matches on either side, nested/overlapping selectors,
post-ignore counts, broadened selectors that would hide a product edit), sibling
identities (mixed-content reorders, nested identities, duplicate/missing keys, exact
key escaping, original paths through ignored same-tag siblings), suppression
locations and provenance, nested-ignore matches, volatile targets, and the
collection, ignore, volatile, whitespace and required-text coverage inventories,
including Markdown escaping and CLI failures that write no report.

[experiment.py](../examples/page_diff/experiment.py) reads [pairs.json](../examples/page_diff/pairs.json),
compares the same labeled pairs with a whole-text baseline and this tool, and
exits nonzero for any missed or unexpected substantive finding. The baseline is
BeautifulSoup `get_text(separator="\n")`, split into lines, with a unified diff;
count added/deleted lines, excluding its headers. No unordered/noise policy is
applied to the baseline. The checked-in [results](../examples/page_diff/experiment-results.json)
are actual deterministic output, not estimated performance.

| Synthetic pair | Raw text changed lines | Semantic findings | Expected substantive findings retained |
| --- | ---: | ---: | ---: |
| Identical | 0 | 0 | 0/0 |
| Reorder only | 8 | 0 | 0/0 |
| Whitespace only | 2 | 0 | 0/0 |
| Reorder and noise | 16 | 0 | 0/0 |
| Price with noise | 16 | 1 | 1/1 |
| Addition with noise | 16 | 1 | 1/1 |
| Removal with noise | 12 | 1 | 1/1 |
| Text with noise | 16 | 1 | 1/1 |
| Attribute with noise | 16 | 1 | 1/1 |
| Attribute only | 0 | 1 | 1/1 |
| Total | 102 | 6 | 6/6 |

**Zero missed and zero unexpected substantive findings** on these ten labeled
synthetic pairs. Raw changed lines and semantic findings are different units;
this is not a percentage speedup or a false-positive-rate estimate. In the
attribute-only pair the text baseline reports zero lines because it does not
observe attributes; the DOM comparator reports the change. No real-site
improvement, adoption, market demand or runtime performance gain is established.

## Existing upstream and boundaries

[changedetection.io](https://github.com/dgtlmoon/changedetection.io) already provides
a complete monitoring product with rich filtering, ignore and notification
features. [Issue 2469](https://github.com/dgtlmoon/changedetection.io/issues/2469)
is historical evidence of a reorder-related report, not proof of a current
upstream bug. This tool provides an independent, tested offline preprocessing/diff
core with strict required selectors, explicitly keyed collections and suppression

Supported inputs are local HTML as interpreted by the tested BeautifulSoup
`html.parser`/Soup Sieve combination and the rules/report version 1. No XML,
CSS layout, browser rendering, arbitrary scripts, website fetching, scheduling,
automatic noise detection or changedetection.io config compatibility is claimed.
