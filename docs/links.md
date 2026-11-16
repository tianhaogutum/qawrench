# qawrench links

`qawrench links` (formerly `lychee-sarif-map`) converts a **bounded, explicitly
validated Lychee JSON format** into SARIF 2.1.0, offline. An optional occurrence
manifest supplies literal source positions; opt-in Markdown mapping can instead read
supported links directly from local UTF-8 files. Every accepted error becomes either
verified occurrence results or a file-only result. Unmapped errors never acquire a
guessed line number. Optional SARIF baseline comparison uses caller-owned occurrence
IDs to track moves separately from new broken-link occurrences, and rejects
mismatched source-root mappings.

No network requests, checker execution, source rewriting, or servers.

## Install

Requires Python 3.11 or newer (tested with Linux CPython 3.14.4). This command has no
runtime dependencies beyond the standard library.

```sh
pipx install qawrench          # or: pip install qawrench
qawrench links --version       # prints the bare qawrench version, e.g. 0.1.0

# Or run without installing:
uvx qawrench links --help
```

`python -m qawrench links` is equivalent to `qawrench links`. The command takes one
positional Lychee JSON report; there are no subcommands. Input paths and `--root` are
relative to the current directory, and `--out`'s parent must exist. The examples
below use files from a qawrench source checkout; run them from its root:

```sh
qawrench links examples/links/lychee.json \
  --manifest examples/links/occurrences.json --root examples/links --out /tmp/links.sarif
```

All `examples/links/` files are **synthetic offline fixtures**, not checker runs.
The example produces four results: two occurrences of the same HTTP 404 URL
(lines 3 and 4), a file-only transport failure, and a file-only timeout. Its
JSON summary on stderr includes:

```json
{"inputFindings":3,"mappedFindings":1,"mappedResults":2,"unmappedFindings":2,"ignoredSuccess":1,"resultCount":4}
```

The actual summary includes additional accounting fields described in [FORMAT.md](links/FORMAT.md).
Without `--out`, SARIF goes to stdout and the summary still goes to stderr.

```sh
# Keep every failure at file level, without a manifest.
qawrench links examples/links/lychee.json --root examples/links

# Validate everything and print summary; no file writes, even if --out is set.
qawrench links examples/links/lychee.json \
  --manifest examples/links/occurrences.json --root examples/links --dry-run

# Opt into CI failure after a valid report is written.
qawrench links examples/links/lychee.json --root examples/links \
  --out /tmp/links.sarif --fail-on-findings
```

Exit codes: `0` valid conversion (selected failure gate passes), `1` findings with
`--fail-on-findings` or new results with `--fail-on-new`, `2` invalid arguments,
input, unsafe paths, or output failure (reported on stderr as `qawrench links: <message>`). Invalid
input leaves an existing output untouched. Output files are replaced atomically;
symlink outputs and overwriting read sources/report/manifest are rejected.
Dry-run performs validation only and never starts scripts or writes reports.

## Occurrence evidence

`examples/links/occurrences.json` is a JSON array:

```json
[
  {"source":"docs/page.md","url":"https://example.invalid/broken","line":3,"column":16},
  {"source":"docs/page.md","url":"https://example.invalid/broken","line":4,"column":22}
]
```

Each `source` must exactly equal an `error_map` key. Each `url` must exactly
equal its original JSON URL, without unescaping, decoding, case folding, remaps,
or trailing-slash changes. Both positions are 1-based; columns count Unicode
code points, including emoji as one and tabs as one. The exact URL literal must
occupy the span beginning at that position on that line. The first example's
emoji would make a UTF-16 column off by one; the tool rejects that mismatch.
An optional `occurrenceId` string identifies a particular use across line/column
moves; its exact contract is described under baseline comparison below.

The manifest uses a **literal span contract**, not a claim that a
substring is semantically a link. The caller identifies intended occurrences;
the tool verifies their text. Manifest mode does not search for missing occurrences or
prove that a manifest is exhaustive. URLs split over lines, relative URLs that
Lychee expanded, and HTML-escaped forms need matching literal evidence and are
otherwise left unmapped. Missing files and invalid supplied evidence are errors.

All report sources (even without manifest locations) must be readable regular
UTF-8 files under `--root`, using canonical relative POSIX paths. Absolute paths,
`..`, dot segments, backslashes, control characters, URI input sources, and
symlink escapes are rejected. Treat the root as a static snapshot while running.

## Markdown occurrence mapping

Use `--markdown` instead of `--manifest` to map supported Markdown links in each
error source. No filename extension is inferred: this flag treats the report's
error sources as Markdown. The library equivalent is `convert(report, root=...,
markdown=True)`. Passing both modes is an error (CLI exit `2`, library
`InputError`); the default and literal manifest contract above are unchanged.

```sh
qawrench links tests/links/fixtures/markdown/lychee.json \
  --root tests/links/fixtures/markdown --markdown --out /tmp/links-markdown.sarif
```

This synthetic fixture produces seven mapped occurrences and one file-only
finding. Its independently written exact regions are in
[`tests/links/fixtures/markdown/expected.json`](../tests/links/fixtures/markdown/expected.json).
For example, with report URL `https://example.invalid/a(b)` and source line:

```markdown
😀 [one](https://example.invalid/a\(b\)) [two](https://example.invalid/a(b))
```

the regions are line `1`, columns `[9,39)` and `[47,75)`. Inline regions cover
the original destination spelling, excluding angle brackets and titles, so
escapes and entities count toward the end column. Matching removes Markdown
backslash escapes and decodes semicolon-terminated character references, then
compares the **entire** destination to the original report URL. It never decodes
percent escapes, changes host case, resolves relative URLs, or changes that URL.

Full `[text][label]`, collapsed `[label][]`, and shortcut `[label]` reference
links resolve against the first supported definition of each label. Labels
collapse whitespace and match without case, including Unicode case folding.
Each reference **use** gets its own full bracketed region; the shared definition
does not become an occurrence. Repeated inline/reference uses remain distinct.
The original report source key is preserved in properties, artifact URIs, and
fingerprints, including when two keys name symlink aliases of the same file.

This is a bounded reader, **not full CommonMark or Lychee parser parity**:

- Supports single-line inline links with balanced/escaped parentheses or angle
  destinations and optional single-line quoted/parenthesized titles, plus
  single-line reference definitions and uses with simple, nonnested labels.
  After a label closes outside code, its valid destination/title is literal
  link syntax: backticks there cannot open prose code spans or hide later links.
  This also applies to inline images, which remain excluded from mapping.
  Code spans starting earlier in prose or labels keep precedence; malformed or
  multiline tails do not receive this protection.
- Supports single-line `<http://…>` and `<https://…>` angle autolinks in the
  same prose, headings and containers, including at line start. The ASCII scheme
  is case-insensitive and must have a nonempty suffix. Whitespace (including
  Unicode whitespace), C0/DEL/C1 controls and nested angle brackets are rejected.
  These regions cover only the URL inside the brackets. Matching preserves
  **every source character**: no entity/HTML or backslash decoding, percent
  decoding, case normalization or punctuation trimming. Autolinks are literal
  syntax, so their backticks cannot open prose code spans; earlier prose code
  openers keep precedence. This recognizes bounded syntax, not URL validity.
- Complete single-line inline raw HTML tags are literal excluded syntax, so
  backticks in quoted attributes cannot open prose code spans and hide later
  inline/reference links or autolinks. Tags use ASCII names beginning with a
  letter and continuing with letters, digits or hyphens. Attribute names begin
  with an ASCII letter, `_` or `:`, continue with letters/digits/`_.:-`, and are
  separated by spaces/tabs. Attributes may be valueless or use `=` with optional
  surrounding spaces/tabs. Single/double quotes close literally (backslashes do
  not escape quotes); `>` and backticks inside them are literal. Unquoted values
  exclude spaces/tabs, CR/LF, quotes, `=`, angles and backticks. Optional trailing
  spaces/tabs and `/` precede `>`; closing tags allow only the name and optional
  spaces/tabs. Earlier prose/label code openers retain precedence. Malformed and
  multiline tags receive no new protection; conservative angle skipping remains.
  Tags at block start retain the existing HTML-block exclusion. This is bounded
  syntax recognition, without HTML tree parsing or attribute URL extraction.
- Complete, non-nested single-line inline HTML comments after prose content
  are excluded tokens: supported inline/reference links and HTTP(S) angle
  autolinks before and after them still map. The opener is exactly `<!--` and
  the first literal `-->` closes it; contents may be empty but cannot contain
  another `<!--`, CR or LF. Quotes and backslashes do not escape delimiters.
  Backticks and link-looking syntax inside comments cannot affect later prose.
  Earlier prose/label code spans and valid destination/title/image, autolink and
  raw-tag attribute syntax retain precedence. Comments at block start, nested,
  incomplete and multiline comments retain conservative block exclusions;
  unsupported syntax may still suppress surrounding mappings. This does not
  add general HTML parsing or multiline comment support.
- Complete single-line inline `<?...?>` processing instructions after prose
  are literal excluded tokens. The opener is exactly `<?` and the first literal
  `?>` closes it; contents may be empty or contain another `<?`, but cannot
  contain CR/LF. Quotes and backslashes do not escape the terminator. Backticks,
  brackets, comments and link-looking contents cannot affect surrounding
  supported inline/reference links or HTTP(S) angle autolinks. Earlier prose or
  label code spans and valid destination/title/image, autolink, raw-tag and
  inline-comment tokens keep precedence. Block-start PIs keep their existing
  exclusion through the first terminator; incomplete/multiline inline forms get
  no new protection and may suppress mappings. No target-name validation,
  general HTML parsing or multiline PI support is added.
- Complete single-line inline `<![CDATA[...]]>` sections after prose are literal
  excluded tokens. The case-sensitive opener is exactly `<![CDATA[` and the
  first literal `]]>` closes it; contents may be empty or contain another opener,
  but cannot contain CR/LF. Quotes and backslashes do not escape the terminator.
  Hidden backticks, brackets, comments and link-looking contents cannot affect
  surrounding supported inline/reference links or HTTP(S) angle autolinks.
  Earlier prose/label code spans and valid destination/title/image, autolink,
  raw-tag, inline-comment and PI tokens retain precedence. Block-start CDATA
  keeps its existing through-terminator exclusion. Incomplete, inexact, escaped
  and multiline inline forms gain no new protection and may suppress mappings.
  This adds no general HTML or multiline CDATA parsing.
- Complete single-line inline declarations after prose are literal excluded
  tokens: exact `<!` followed immediately by an ASCII uppercase letter, through
  the first literal `>` on that line. The tail after the required letter may be
  empty (`<!A>`); quotes, backslashes and nested openers do not delay closure.
  Hidden backticks, brackets, comments and link-looking contents cannot affect
  surrounding supported inline/reference links or HTTP(S) angle autolinks.
  Earlier prose/label code and valid destination/title/image, autolink, raw-tag,
  comment, PI and CDATA syntax keep precedence. Block-start declarations retain
  their existing through-`>` exclusion; incomplete, inexact, escaped and multiline
  inline forms gain no new protection and may suppress mappings. This adds no
  DTD, entity or general HTML parsing.
- Reads prose and ATX headings at top level and inside nested blockquotes and
  unordered/ordered lists. Explicit list continuation lines supply the item's
  content indentation. Each container prefix allows up to three
  leading spaces; list markers are `-`, `+`, `*`, or 1–9 digits followed by `.` or
  `)`, with 1–4 padding spaces. Extra padding becomes indented content. Original
  source columns include all removed prefixes; tabs are never expanded.
- An active list or quote paragraph can continue with omitted list indentation
  or `>` markers, including nested and mixed list/quote containers. Only ordinary
  prose continues it; blank lines, headings/setext underlines, fences, HTML or
  block-start/unsupported comments, list markers, and indented/tab-based content
  end eligibility.
  Definition-looking lazy text remains prose and cannot define a reference.
  Code spans retain their scope across accepted lazy lines; images stay excluded.
- Each ATX heading is a separate inline block. Code-span backticks cannot pair
  across a heading's entry or exit, including adjacent headings inside the same
  list/quote container. Cross-line code spans within one prose paragraph still
  mask links across supported explicit and lazy continuation lines.
- Skips fenced/indented code, code spans, images, comments, raw HTML tags/blocks,
  and thematic breaks, including inside containers. Indented and ordinary HTML
  blocks are conservatively skipped through the next blank line; comments and
  raw text HTML blocks through their closing delimiters. Exclusions and code
  spans end when their container ends; definitions remain document-wide.
  Nested labels suppress further mapping on that line.
- Tab-based container indentation is unsupported. Rejected lazy lines stay
  skipped until a blank line or another explicit
  container. Block-like lazy lines are conservatively rejected even where a
  full Markdown parser could treat them as paragraph text (for example `2. item`).
  Multiline link constructs, nested labels, other angle forms (including email
  autolinks), bare URLs, HTML links,
  relative URL expansion, and extension syntax are outside the contract.
  Unsupported syntax can suppress other mappings; this mode is not exhaustive.

The independent nested-container and repeated-reference fixtures can be run with:

```sh
qawrench links tests/links/fixtures/markdown-containers/lychee.json \
  --root tests/links/fixtures/markdown-containers --markdown --out /tmp/links-containers.sarif
```

Expected: 15 mapped occurrences (9 inline destinations and 6 reference uses)
plus 2 file-only findings. For `- 😀 [one](https://x.invalid/)`, the destination
is on line 1 at columns `[11,29)`, including the list prefix and emoji in the
column count. Exact expected regions for both source files are in
[`tests/links/fixtures/markdown-containers/expected.json`](../tests/links/fixtures/markdown-containers/expected.json).

Lazy quote fixtures have independent original-source regions too:

```sh
qawrench links tests/links/fixtures/markdown-lazy-quotes/lychee.json \
  --root tests/links/fixtures/markdown-lazy-quotes --markdown --out /tmp/links-lazy-quotes.sarif
```

Expected: 15 mapped occurrences and 2 file-only findings. In `basic.md`, the
destination in `> start [one](https://x.invalid/)` is at line 1, columns `[15,33)`;
`continued [two](https://x.invalid/)` maps at line 2, columns `[17,35)`.
The indented code after the blank line stays excluded. Nested/partial markers,
Unicode, repeated links, references, and cross-line code spans are covered in
[`tests/links/fixtures/markdown-lazy-quotes/expected.json`](../tests/links/fixtures/markdown-lazy-quotes/expected.json).

Lazy list fixtures cover omitted and partial indentation, including mixed nested
quotes/lists:

```sh
qawrench links tests/links/fixtures/markdown-lazy-lists/lychee.json \
  --root tests/links/fixtures/markdown-lazy-lists --markdown --out /tmp/links-lazy-lists.sarif
```

Expected: 17 mapped occurrences and 2 file-only findings. The two destinations
in `basic.md` have the same `[15,33)` and `[17,35)` columns as the quote example.
The code line has **six** spaces: the list item's two-space indentation plus
four spaces for code. Four absolute spaces can still be list prose. A blank
line ends lazy eligibility; a following unindented block can start outside the
list, including a real reference definition. Definition-looking text within an
active lazy paragraph still cannot define or override a reference. Exact regions
are in [`tests/links/fixtures/markdown-lazy-lists/expected.json`](../tests/links/fixtures/markdown-lazy-lists/expected.json).

Heading-boundary fixtures exercise code spans entering/leaving headings,
adjacent headings, mixed list/quote containers, and repeated references:

```sh
qawrench links tests/links/fixtures/markdown-heading-boundaries/lychee.json \
  --root tests/links/fixtures/markdown-heading-boundaries --markdown --out /tmp/links-headings.sarif
```

Expected: 10 mapped occurrences and 2 file-only findings. In `basic.md`, the
backticks on the surrounding prose lines cannot mask the heading's destination
on line 2, columns `[11,29)`. The independent Unicode/source regions are in
[`tests/links/fixtures/markdown-heading-boundaries/expected.json`](../tests/links/fixtures/markdown-heading-boundaries/expected.json).

Title-boundary fixtures cover backticks in quoted/parenthesized titles, image
exclusions, repeated references, and original Unicode/container columns:

```sh
qawrench links tests/links/fixtures/markdown-title-boundaries/lychee.json \
  --root tests/links/fixtures/markdown-title-boundaries --markdown --out /tmp/links-titles.sarif
```

Expected: 11 mapped occurrences and 2 file-only findings. In `basic.md`,
``[one](https://x.invalid/ "a`") [two](https://x.invalid/ "b`")`` maps both
destinations on line 1 at columns `[7,25)` and `[38,56)`. Exact regions are in
[`tests/links/fixtures/markdown-title-boundaries/expected.json`](../tests/links/fixtures/markdown-title-boundaries/expected.json).

For report URL `https://example.invalid/a?x=1&y=2`, this source:

```markdown
See <https://example.invalid/a?x=1&y=2> and <https://example.invalid/a?x=1&y=2>.
```

produces two occurrence results at line 1, columns `[6,39)` and `[46,79)` with
`--markdown`. Default mode yields one file-only result. Code, HTML/comments,
images and unused reference definitions remain excluded. Run the synthetic
autolink fixture with:

```sh
qawrench links tests/links/fixtures/markdown-autolinks/lychee.json \
  --root tests/links/fixtures/markdown-autolinks --markdown --out /tmp/links-autolinks.sarif
```

Expected: 9 mapped occurrences and 2 unmatched file-only findings. Independent
regions, including emoji, nested containers and lazy continuations, are in
[`tests/links/fixtures/markdown-autolinks/expected.json`](../tests/links/fixtures/markdown-autolinks/expected.json).

Inline HTML boundary fixtures combine quoted `>`/backticks with repeated
inline/reference links, an autolink, explicit/lazy containers and Unicode:

```sh
qawrench links tests/links/fixtures/markdown-html-boundaries/lychee.json \
  --root tests/links/fixtures/markdown-html-boundaries --markdown --out /tmp/links-html.sarif
```

This emits ten mapped regions plus two unmatched file-only results; default mode
emits four file-only results. Original-source coordinates are independently
specified in
[`tests/links/fixtures/markdown-html-boundaries/expected.json`](../tests/links/fixtures/markdown-html-boundaries/expected.json).

Inline-comment fixtures preserve prose on both sides of excluded contents:

```sh
qawrench links tests/links/fixtures/markdown-inline-comments/lychee.json \
  --root tests/links/fixtures/markdown-inline-comments --markdown --out /tmp/links-comments.sarif
```

Expected: 13 mapped occurrences plus two unmatched file-only findings. For
`[one](https://x.invalid/) <!-- hidden --> [two](https://x.invalid/)`, both
URL destinations map on line 1 at columns `[7,25)` and `[49,67)`. Independent
regions for repeated references/autolinks, Unicode and explicit/lazy containers
are in
[`tests/links/fixtures/markdown-inline-comments/expected.json`](../tests/links/fixtures/markdown-inline-comments/expected.json).

LF and CRLF files retain the same Unicode code-point coordinates.

A report URL without supported matches still gets one file-only result, never a
guessed region. If only some uses are supported, only those regions are emitted.
Missing/unreadable sources and unsafe paths still fail before replacing output.
Run property `occurrenceMapping` identifies `markdown-bounded-v1` or the default
`literal-manifest`. The existing strict Lychee JSON profile still applies.

## Optional source context

Add `--source-context` with either `--manifest` or `--markdown` to include
original source lines for mapped locations:

```sh
qawrench links examples/links/lychee.json \
  --manifest examples/links/occurrences.json --root examples/links --source-context \
  --out /tmp/links-context.sarif
```

The library equivalent is `convert(..., source_context=True)`. The default is
off and leaves existing SARIF output unchanged. Each mapped `physicalLocation`
gains `contextRegion` with inclusive `startLine`/`endLine` and `snippet.text`.
It contains exactly the complete original lines intersecting the existing
precise `region`, without neighboring lines. Existing CRLF, LF and CR line
terminators are retained, with no synthetic final newline. Unicode, combining
characters, tabs and Unicode separators remain verbatim. End-exclusive column 1
on a later line does not intersect that line. Snippets use the same decoded
UTF-8 source snapshot as occurrence validation; files are never reopened for
context. The current literal and bounded Markdown readers emit single-line
regions, including reference uses; this option does not add multiline parsing.

Two occurrences on one line receive the same complete-line context while
keeping their distinct URL regions and fingerprints. File-only/unmatched
results and unlocated aggregate warnings never gain a guessed line or snippet.
Without mapped evidence, the option adds nothing. Source/path errors and invalid
manifest positions still fail validation before output replacement.

**Opt-in disclosure:** complete lines can contain surrounding private source,
comments or other URLs. Review them before sharing a report. Snippets are JSON
string data, never executed or interpreted as Markdown/HTML by this converter;
downstream viewers should treat them as source text. Context does not affect
stable IDs, fingerprints, baseline classifications or failure gates. Reports
with and without context can be compared in either direction. Historical
context is checked structurally without reading old source; its text is not
proof of historical contents.

## SARIF baseline comparison

Pass `--baseline previous.sarif` or `convert(..., baseline=previous_sarif)` to
compare with a previous report from this adapter. It must contain exactly one
run with the same tool profile, column kind, Lychee dialect, occurrence mapping
mode, and **identical resolved `%SRCROOT%` mapping**. A different checkout root,
extra URI base, altered artifact URI/base, duplicate occurrence identity, or
inconsistent result/fingerprint fails with exit `2` / `InputError`. No implicit
root remapping or cross-file matching is performed. Historical source files do
not need to exist; only current evidence is read and verified.

To track a moved occurrence, add a stable, nonempty `occurrenceId` to its manifest
entry, for example:

```json
{"source":"page.md","url":"https://example.invalid/broken","line":3,"column":1,"occurrenceId":"install-guide"}
```

Keep that ID when updating its verified line/column; give a newly added use a
different ID, even if its URL is identical. IDs are case-sensitive UTF-8 strings
without C0/DEL control characters, unique within each exact source/URL pair.
The caller maintains their continuity; the tool cannot detect an incorrectly
reused ID. Source or URL changes create a new identity. Adopting/removing IDs
also creates new identities; no positional fallback overrides a supplied ID.

Current results receive SARIF `baselineState` values `new`, `unchanged`, or
`updated`. A matched ID with a changed region is `updated` with
`properties.baselineMatch: "moved"` and `baselinePreviousRegion`; a changed
diagnostic/status at the same occurrence is `updated`. The existing positional
fingerprint remains unchanged in definition; identified results additionally
carry `lycheeStableOccurrence/v1`, which excludes position and failure category.

Without IDs (including Markdown mode and previous-round reports), identity uses
exact source/URL/region; line moves cannot be inferred and count as new/absent
unless an exact position still matches. File-only findings compare by source/URL;
the unsupported aggregate compares as one summary, retaining count changes.
`summary.baselineComparison` contains `new`, `unchanged`, `updated`, `moved`
(a subset of `updated`), and `absent`. Absent means missing from the current
evidence, **not proven fixed**: no stale result or guessed historical region is
emitted. Partial manifests and bounded Markdown remain nonexhaustive.

This synthetic example moves `install-guide` from line 3 to 8, adds `new-guide`
on line 9 for the same URL, and drops `retired-guide`:

```sh
mkdir -p /tmp/links-baseline-root
cp tests/links/fixtures/baseline/before.md /tmp/links-baseline-root/page.md
qawrench links tests/links/fixtures/baseline/lychee.json \
  --root /tmp/links-baseline-root --manifest tests/links/fixtures/baseline/before.json \
  --out /tmp/links-before.sarif
cp tests/links/fixtures/baseline/after.md /tmp/links-baseline-root/page.md
qawrench links tests/links/fixtures/baseline/lychee.json \
  --root /tmp/links-baseline-root --manifest tests/links/fixtures/baseline/after.json \
  --baseline /tmp/links-before.sarif --out /tmp/links-after.sarif
```

Expected comparison: `{"new":1,"unchanged":0,"updated":1,"moved":1,"absent":1}`.
Dry-run validates the baseline too. Invalid input leaves existing output
untouched, and the CLI rejects overwriting the baseline (including hard-link
aliases). `--fail-on-findings` still gates **all current results**; baseline
comparison does not suppress findings. A compared report can be the next baseline.

For CI that should fail only on newly observed broken-link occurrences, use
`--fail-on-new` with a valid `--baseline`:

```sh
# After preparing the before/after example above: exits 1 for new-guide.
qawrench links tests/links/fixtures/baseline/lychee.json \
  --root /tmp/links-baseline-root --manifest tests/links/fixtures/baseline/after.json \
  --baseline /tmp/links-before.sarif --out /tmp/links-after.sarif --fail-on-new

# Same current evidence compared with that report: exits 0 (two unchanged).
qawrench links tests/links/fixtures/baseline/lychee.json \
  --root /tmp/links-baseline-root --manifest tests/links/fixtures/baseline/after.json \
  --baseline /tmp/links-after.sarif --fail-on-new --dry-run
```

This gate returns `1` exactly when `summary.baselineComparison.new > 0`, counting
occurrences rather than distinct URLs. Unchanged results, moved stable IDs,
diagnostic updates, and absent-only comparisons return `0`. All current results
still appear in the complete SARIF, written before a gate failure. Dry-run uses
the same exit decision and prints the summary without writing SARIF. A valid
empty baseline is allowed. Missing/invalid/incompatible baselines and output
failures return `2`; existing output is preserved on invalid input.
`--fail-on-new` and `--fail-on-findings` are mutually exclusive. Unidentified
line moves still count as new according to the identity rules above; the gate
does not infer moves or prove a manifest exhaustive.

## Format support and differences from Lychee

The [historical issue #1637](https://github.com/lycheeverse/lychee/issues/1637)
provides the actual JSON example used by an end-to-end test. The tested profile
is based on the `lychee-v0.18.1` serializers. This is **not a general converter
for every Lychee release or error type**. See [FORMAT.md](links/FORMAT.md) for the exact
schema and rejection conditions.

[Lychee](https://github.com/lycheeverse/lychee) already checks links and provides
report formats, including JSON/JUnit/Markdown in its evolving releases. The
issue also records later upstream work on line/column output. This adapter adds
an offline SARIF bridge with literal occurrence evidence or opt-in bounded
Markdown mapping; it does not claim that current upstream lacks location reporting.
No Lychee implementation is copied or executed.

Important bounds:

- Supports uncached `Network error` (HTTP code or transport detail) and timeout
  error records in this profile. Unrecognized text or additional fields fail.
- Lychee v0.18.1 omits individual unsupported responses from its maps. A positive
  `unsupported` counter becomes **one aggregate warning**, with the original
  count and no invented URL/location. It is not one result per unsupported URL.
- Nonzero cached, unknown-status, or redirect counters; nonempty suggestions;
  and inconsistent/deduplicated counters are explicitly rejected.
- Remote input documents, absolute input source paths, and newer response fields
  such as `span`, `redirects`, `remap`, or `duration` are outside this profile.
- SARIF artifact URIs are percent-encoded relative paths with `%SRCROOT%` set to
  the local root's file URI. Fingerprints exclude that absolute base and status
  detail, but include category, relative source, original URL, and exact region.
  They are stable across report/manifest ordering and diagnostics changes.
  Position fingerprints change across moves; optional stable occurrence IDs
  support the explicit baseline comparison described above.

## Library and tests

```python
from qawrench.links import convert, load_json

sarif = convert(load_json("examples/links/lychee.json"), root="examples/links",
                manifest=load_json("examples/links/occurrences.json"))
summary = sarif["runs"][0]["properties"]["summary"]
assert summary["mappedResults"] == 2
```

`convert` returns a dictionary and never writes. Invalid data raises `InputError`.

From a qawrench checkout:

```sh
uv run --extra test pytest tests/links -q
```

The suite covers the historical issue JSON, repeated inline/reference/autolink occurrences,
escaped parentheses, exact Markdown source regions and URI identity, Unicode/CRLF,
nesting and continuations of list/blockquote containers, container code/HTML
exclusions, independently specified repeated references and original columns,
preserved unmapped failures, status categories, accounting, path/symlink escapes,
malformed data, dry-run, CI exit codes, atomic output failure, baseline move/new/
absent identity, source-root mismatch refusal, and baseline CLI use.
The focused `tests/links/test_fail_on_new.py` contracts also cover new-only CI gating,
unchanged/moved/updated/absent passes, dry-run, empty baselines, invalid evidence,
output failures and mutually exclusive gates. The official SARIF 2.1.0 schema was
checked during the original development but is not bundled; regular tests need no
schema or third-party packages.

The SARIF `tool.driver` object (`"name": "lychee-sarif-map"`, `"version": "0.1.0"`)
is kept unchanged so that baselines produced by the standalone tool stay compatible:
baseline comparison requires an identical `tool` object.

No real-site improvements, adoption, demand, or performance gains were measured.
Compatibility beyond the explicitly tested format is unverified.

## License

The adapter is MIT licensed; see [LICENSE](../LICENSE). See
[THIRD_PARTY.md](../THIRD_PARTY.md) for format references and the attributed
historical fixture.
