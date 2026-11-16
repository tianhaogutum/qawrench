# qawrench har

`qawrench har` (formerly `har-replay-doctor`) maintains **offline HAR 1.2 fixtures**
(embedded JSON or bounded self-contained Playwright `.har.zip`). It plans exact origin
replacements and explicit query-key and request-header row removals, explains
collisions, and refuses to apply rules that group different responses under the same
rewritten request key. An explicit request inventory can also require exactly one
fixture match per target request before a rewritten HAR is published. It can also
select complete entries using that inventory into a smaller HAR, retaining only
selected referenced payloads for ZIP input. A read-only selection plan reports
original entry IDs and counts before selection, without copying request values.
Selection can optionally require a reviewed plan to agree with the current inputs.

Playwright already provides HAR replay, minimal recording and editable fixtures.
[Its docs](https://playwright.dev/docs/mock#replaying-from-har) describe strict
URL/method/POST matching and request-header tie-breaking. This command adds an
offline, reviewable maintenance step; it does not replace the replay engine.
[Issue #21405](https://github.com/microsoft/playwright/issues/21405) is historical
use-case context, not a claim of a current unresolved upstream bug.

## Install

Requires Python 3.11+ and a local filesystem supporting hard links (tested on
Linux with CPython 3.12.13 and 3.14.4). This command has no runtime dependencies
beyond the standard library.

```sh
pipx install qawrench          # or: pip install qawrench
qawrench har --version         # prints the bare qawrench version, e.g. 0.1.0

# Or run without installing:
uvx qawrench har --help
```

`python -m qawrench har` is equivalent to `qawrench har`. Normal execution is
offline. No browser, Playwright package or server is required.

The subcommands are `export-inventory`, `select-requests`, `plan-selection`,
`embed-forms`, `plan`, `apply` and `preflight`. Errors are printed as
`qawrench har: <message>` and exit 2.

## Working examples

All included data is **synthetic**, authored offline, and contains no production
requests. The rules in `examples/har/rules.json` are:

```json
{
  "origins": {"https://stage.example": "http://fixture.test"},
  "dropQueryKeys": ["cacheBust"]
}
```

Use `plan` for the dry run. There is no implicit apply and no
execution of fixture/rules content. From the qawrench checkout root:

```sh
qawrench har plan examples/har/input.har \
  --rules examples/har/rules.json --out /tmp/har-plan.json
qawrench har apply examples/har/input.har \
  --rules examples/har/rules.json --out /tmp/har-output.har
```

Use fresh output paths: **every existing output is refused**, including plan
files, input aliases, hard links and dangling symlinks. The program never
modifies the input or rules. It does not create missing output directories.
For another run, choose a new filename. The example paths are relative to a
qawrench source checkout.

For entry 0, the rewrite is:

```text
https://stage.example/items?tag=red&cacheBust=101&tag=blue&empty=&q=a%20b
http://fixture.test/items?tag=red&tag=blue&empty=&q=a%20b
```

The example reports 4 entries, 3 changed, and 0 collision groups. The similar
suffix host `stage.example.evil.test` stays unchanged. POST text and base64
response content stay unchanged. These example rules change only `request.url`
and explicitly named rows in `request.queryString`. Every other JSON field is
preserved, including extensions, headers, cookies, redirect locations and body strings.
JSON whitespace/escaping of the output file is reformatted, so this is payload
byte preservation, not preservation of the original HAR file's serialization.

To remove explicitly named request-header rows, use the optional rule:

```json
{"dropRequestHeaders": ["X-Request-ID", "Authorization"]}
```

The synthetic header example has three requests, mixed-case duplicate rows,
text/binary bodies and separate cookie/query/response metadata:

```sh
qawrench har plan examples/har/request-headers.har \
  --rules examples/har/request-header-rules.json --out /tmp/har-header-plan.json
qawrench har apply examples/har/request-headers.har \
  --rules examples/har/request-header-rules.json --out /tmp/har-header-output.har
```

Expected: 2 changed entries, 4 removed rows: `authorization` 2 and
`x-request-id` 2. Every matching row is removed, on all origins; retained rows
keep their original order, spelling, values and extensions. No body is inferred,
reconstructed or reserialized by header removal; existing body validation still
runs. All response headers/cookies and other metadata, including recorded
`headersSize`, stay unchanged. Only explicit `request.headers` rows are removed:
URL/query fields, `request.cookies`, Cookie header contents, payload fields and
extensions are separate. A Cookie header row is removed only if `Cookie` is
explicitly named. This is **not a comprehensive secret scrubber** or a
replay-equivalence guarantee; removing headers can affect replay selection.

## Request-match preflight

Use `preflight` to compare a saved HAR with an explicit request inventory. With
`--rules`, it checks proposed fixture URLs **before rewriting**; without rules,
it checks the saved URLs as they are. Inventory URLs always describe the actual
target requests and are never normalized. For the included synthetic fixtures:

```sh
qawrench har preflight examples/har/input.har \
  --inventory examples/har/requests.json --rules examples/har/rules.json \
  --out /tmp/har-preflight.json
qawrench har plan examples/har/input.har \
  --inventory examples/har/requests.json --rules examples/har/rules.json \
  --out /tmp/har-inventory-plan.json
qawrench har apply examples/har/input.har \
  --inventory examples/har/requests.json --rules examples/har/rules.json \
  --out /tmp/har-inventory-output.har
```

Expected preflight summary: 4 requests, 4 matched, 0 missing, 0 ambiguous,
`passed: true`, exit 0. Omitting `--rules` from the preflight command reports
3 missing and 1 matched, writes its diagnostics, and exits 2. Choose a fresh
output path for each command. No HAR is written by `preflight` or `plan`.

Inventory format (schema version 1):

```json
{
  "schemaVersion": 1,
  "requests": [
    {"id": "items-get", "method": "GET", "url": "http://fixture.test/items"},
    {"id": "items-post", "method": "POST", "url": "http://fixture.test/items",
     "postData": {"mimeType": "application/json", "text": "{\"id\":2}"}}
  ]
}
```

`requests` must be nonempty, and `id` must be a unique nonempty string. Required
`method` and `url` follow the HAR validation rules. Optional `postData` uses the
same embedded text/base64 format as HAR requests; omitted postData means empty
bytes. Optional `bodySize` is checked as in HAR; a positive value without an
embedded body is rejected. Optional name/value `headers` are validated but do
not select matches. Other top-level/request fields, duplicate JSON keys, unknown
schema versions, attached bodies and params-only bodies are errors. Separate
inventory IDs may describe repeated identical requests; each is checked
independently against the full fixture, without consuming entries.

Reports use `matchModel: "exact-url-method-body-v1"`: literal URL, exact method,
and exact body bytes on **all methods**, as in the existing collision model.
Each result includes `requestId`, `requestIndex`, `key`, `status`, zero-based
`entryIndexes` and JSON Pointer `entryIds` into the original saved HAR, such as
`/log/entries/0`. Canonical HAR and inventory SHA-256 digests identify the inputs.
For example, two fixtures with the same key yield:

```json
{"requestId":"items-get","status":"ambiguous","entryIndexes":[0,1],
 "entryIds":["/log/entries/0","/log/entries/1"]}
```

Zero matches yield `status: "missing"` and empty entry lists; one yields
`"matched"`. Two or more are ambiguous **even with identical responses**. Any
missing/ambiguous request makes `passed: false` and preflight exits 2 while
writing the report. Malformed input or I/O errors exit 2 without a report.

Each result also has `nearMatches`. Only missing requests list candidates:
all entries with the same literal URL but a different method, exact body bytes,
or both, in original HAR order. With rules, this uses the projected fixture URL;
inventory URLs stay unchanged. Each candidate identifies its `entryIndex`,
original `entryId`, and `differingFields` (`"method"`, `"body"`, or both in that
order). Body comparison decodes text/base64 to bytes on all methods; whitespace
and Unicode spelling matter, while MIME type and headers do not select matches.
Matched/ambiguous requests and misses with no same-URL entries have empty
`nearMatches`. Different URL spellings are not suggested.

For a runnable missing-request example:

```sh
qawrench har preflight examples/har/input.har \
  --inventory examples/har/near-match-requests.json --rules examples/har/rules.json \
  --out /tmp/har-near-matches.json
```

This writes diagnostics and exits 2: all four requests remain missing with empty
`entryIndexes`/`entryIds`. The first three suggest `/log/entries/1` for method,
body, and both differences respectively; the unrelated URL has no candidates.
The first result includes:

```json
{"requestId":"method-only","status":"missing","entryIndexes":[],"entryIds":[],
 "nearMatches":[{"entryIndex":1,"entryId":"/log/entries/1","differingFields":["method"]}]}
```

Candidates explain failures; they never count as matches, change summary counts,
or allow apply. The same diagnostics appear in `plan --inventory` and in the
guarded apply error. No candidate body content is added to the report.

`plan --inventory` embeds the same report as `requestPreflight` and sets
`safeToApply` false on failure. `apply --inventory` recomputes the check and
refuses to create an output HAR on failure, with request IDs and entry paths in
stderr. Inventory inputs receive the same no-overwrite protection as HAR/rules.
Existing commands without `--inventory` retain their original collision policy.
Preflight only checks the supplied requests; it does not require every HAR
entry to be used. A passing standalone preflight does not certify the rewrite's
separate response-collision policy; use `plan` to check both. It does not
simulate Playwright's header tie-breaking, multipart matching or redirects and
does not guarantee browser replay equivalence.

## Export an inventory from a HAR

`export-inventory` creates a schema-version-1 inventory from the supported
embedded HAR subset, without rewriting URLs or choosing new body bytes:

```sh
qawrench har export-inventory examples/har/export-source.har \
  --out /tmp/har-exported-requests.json
qawrench har preflight examples/har/export-source.har \
  --inventory /tmp/har-exported-requests.json --out /tmp/har-export-preflight.json
```

Export exits 0 and reports `requests: 7` for this synthetic 9-entry HAR.
Preflight writes its report and exits 2: 5 requests matched uniquely, 2 are
ambiguous, and none are missing. Export records distinct requests; it does not
remove HAR duplicates or certify unique replay matches, even when responses
are identical. Different responses do not prevent export either; use plan to
inspect response conflicts.

Keys are literal URL, exact method (including case), and decoded exact body
bytes on all methods. Equivalent text/base64 bodies collapse to one request;
different URL spellings, methods or bytes remain distinct. Missing postData,
empty text and base64 for empty bytes share the empty-body key. Requests appear
in first-entry order. Each ID is the first entry's JSON Pointer, such as
`/log/entries/0`; IDs refer to that source ordering and may change if entries
are reordered. There are no timestamps, hashes or normalized URLs in the export.

Each request contains only `id`, `url`, `method`, and the first entry's complete
`postData` object when present. Text/base64 spelling, MIME type, params and
extensions in that object are copied unchanged for JSON input. ZIP attachment
resolution is described below. Other request metadata,
including headers and bodySize, and all responses are omitted. If the first
entry has no postData, the exported request omits it. An explicit empty body
on the first entry remains explicit. Headers and MIME type do not affect key
deduplication. Exported artifacts can contain sensitive URLs and body content;
there is no automatic redaction.

The whole HAR is validated before deduplication, including responses and later
duplicates. Loose attached bodies, params-only bodies, malformed JSON/base64 and
invalid multipart captures fail with exit 2 and no inventory publication.
Export never infers bytes from params, file paths, sizes or headers. An empty
HAR fails with an actionable error because version-1 inventories must be
nonempty. Every export passes `validate_inventory` before publication. The
command accepts no rules or inventory flags and uses the same atomic
no-overwrite output protection as apply; the input file stays byte-for-byte
unchanged. Use a fresh output path on each run.

## Plan an inventory selection without writing a HAR

`plan-selection` accepts the same supported JSON and bounded self-contained HAR
ZIP inputs as `select-requests`. It validates the entire source and strict
nonempty inventory, requires every selector to match exactly one original entry,
and writes only a JSON membership report. No rewrite rules are accepted:

```sh
qawrench har plan-selection examples/har/selection-source.har \
  --inventory examples/har/selection-requests.json --out /tmp/har-selection-plan.json
qawrench har select-requests examples/har/selection-source.har \
  --inventory examples/har/selection-requests.json --plan /tmp/har-selection-plan.json \
  --out /tmp/har-planned-selection.har
```

Use fresh output paths. Both commands exit 0 with `totalEntries: 6` and
`selectedEntries: 2`. The report is schema version 1 with fixed
`tool: "har-replay-doctor"` (kept for compatibility with existing plans), `matchModel: "exact-url-method-body-v1"`,
`harSHA256`, `inventorySHA256`, and:

```json
{
  "selectedEntryIndexes": [1, 3],
  "selectedEntryIds": ["/log/entries/1", "/log/entries/3"],
  "summary": {"totalEntries": 6, "selectedEntries": 2}
}
```

Indexes are original zero-based indexes, and IDs are JSON Pointers into the
original HAR bound to `harSHA256`; selection output subsequently renumbers its
entries. Reversed/repeated selectors retain capture order and never duplicate an
entry. Missing or ambiguous selectors exit 2 with **no report and no HAR**,
including identical-response duplicates. Errors identify selector index, status
and match count without inventory IDs, URLs or payload values. Malformed inputs
also produce no report; existing validation diagnostics may contain source
values. This differs from `preflight`, which writes detailed failed-match reports.

For ZIP input, `summary` additionally contains `totalPayloadMembers`,
`retainedPayloadMembers` and `discardedPayloadMembers`. These count distinct
archive members excluding the manifest. Retained members are exactly the union
of selected request/response `_file` references; shared and zero-byte members
count once. All other members, including unused attachments, count as discarded.
Embedded-only selections retain zero payload members. Planning shares this
reference calculation with selection and checks all unselected entries/references
and unused members under the unchanged archive limits before reporting success.

Digests use SHA-256 of canonical JSON: sorted object keys, ASCII escapes, compact
separators and no nonfinite numbers, as in preflight. `inventorySHA256` includes
inventory IDs and selector ordering, though neither appears in the report.
**For ZIP input, `harSHA256` identifies the resolved embedded HAR, not the raw ZIP
or original attached manifest.** It includes all resolved HAR entries/metadata,
but does not bind unused member bytes/names, ZIP metadata or compression. Different
archives can share this digest and have different payload-member counts. JSON
formatting changes do not affect either canonical digest. These are identity
checks, not secret-safe hashes or an attestation of raw archive identity.

The report contains only fixed schema/tool/model identifiers, digests, original
indexes/IDs and counts. It excludes paths, URLs, user inventory IDs, request/body
and response values, attachment names, rules and per-selector diagnostics. CLI
stdout contains command/output path and counts, as with selection. Inputs remain
byte-for-byte unchanged. Publication uses the same atomic no-clobber JSON writer,
protects both inputs, rejects input/inventory aliases and existing/dangling targets,
and cleans temporary files on failure. No members are extracted, loose paths read
or HTTP calls made. Local hard-link support is required.

A plan predicts membership and counts for the inputs checked. It does not publish
or serialize a selected HAR, check its eventual output size/path, or reserve a
future output. No browser replay-equivalence claim is made.

With `select-requests --plan REPORT`, selection fully validates and recomputes
membership, then requires the supplied JSON to equal the entire current report.
Every field is required and unknown fields at any level are refused. Schema/tool/
model, both digests, ordered original indexes/IDs and all counts must agree;
JSON types must match exactly (booleans/floats cannot substitute for integers).
Duplicate keys, nonfinite numbers, malformed JSON and stale or tampered plans
are refused. Object key order and JSON formatting are irrelevant; array order
matters. Changes to source metadata, inventory IDs or selector order invalidate
the plan even when the selected entries would be the same. Extraction always
recomputes membership from the HAR/inventory, never from supplied indexes.

All three inputs must differ, including symlink and hard-link aliases. The
existing atomic no-clobber writer protects the HAR, inventory and plan; failure
leaves their bytes intact and publishes no HAR. Guarded loading, validation,
comparison and publication failures exit 2 with empty stdout and the fixed,
value-free diagnostic `guarded selection refused; validate inputs, plan and output path`.
This deliberately hides detailed malformed-input diagnostics; inspect the inputs
separately with existing commands if needed. Calls without `--plan` retain their
existing selection behavior and diagnostics. The guard adds no loose reads,
extraction, HTTP, rewrite rules or output reservation.

For ZIP, the guard checks the resolved-HAR digest and all three payload-member
counts. Adding/removing an unused member changes the counts and invalidates the
plan. Same-count changes to unused member bytes/names, equivalent reference
spellings and ZIP metadata/compression can still pass. This is a reviewed
membership/identity check under the documented model, **not raw ZIP identity**.
A matching plan also does not guarantee that later serialization or publication
will succeed, nor prove browser replay equivalence.

## Select requests into a smaller HAR

`select-requests` copies only entries explicitly selected by an existing strict
request inventory. It accepts embedded JSON or supported self-contained HAR ZIP
input, with no rewrite rules:

```sh
qawrench har select-requests examples/har/selection-source.har \
  --inventory examples/har/selection-requests.json --out /tmp/har-selected.har
qawrench har preflight /tmp/har-selected.har \
  --inventory examples/har/selection-requests.json --out /tmp/har-selected-preflight.json
```

Use fresh output paths. Selection exits 0 and reports `selectedEntries: 2` and
`totalEntries: 6`, plus command/output path, without request or response values.
The inventory has three selectors in reversed/repeated order; preflight then
exits 0 with all three matched. The HAR retains the two selected entries in
their **original capture order**, once each.

Every selector must match exactly one original entry using the same
`exact-url-method-body-v1` preflight model: literal URL, exact method including
case, and decoded body bytes on every method. Text/base64 for the same bytes
match; omitted/explicit empty bodies match. Headers, MIME type and inventory
IDs do not disambiguate keys. Repeated selectors must have distinct IDs as
required by the inventory schema; they neither consume nor duplicate matches.
Missing and ambiguous selectors fail with exit 2 and no output, even when
duplicate entries have identical responses. Match errors report selector index,
status and match count, without URLs, IDs or body/response values. Existing
validation diagnostics are unchanged. Use `preflight` separately for detailed
diagnostics, which can contain request values.

The whole HAR and inventory are validated before selection, including late
unselected entries and their responses. Existing body, MIME, size and encoding
boundaries remain strict. Unselected valid keys may repeat, as in preflight;
selected keys must be unique. This adds no exception to apply's collision gate.

The output preserves all top-level and `log` metadata, including creator,
browser, comments, pages and extensions, and each selected **complete entry**.
It keeps bodies, binary/base64 spelling, params, headers, cookies, timestamps,
timings and extensions as original JSON values. Unused page metadata is retained.
JSON whitespace/escaping may change; input files stay byte-for-byte unchanged.
This minimizes the entry set, not the fields within entries, and is not a
Playwright minimal-mode conversion, redaction or browser replay-equivalence claim.

Selection also accepts the bounded self-contained `.har.zip` subset below and
requires a fresh `.har.zip` output for ZIP input:

```sh
qawrench har select-requests recording.har.zip \
  --inventory requests.json --out selected.har.zip
qawrench har preflight selected.har.zip \
  --inventory requests.json --out selected-preflight.json
```

Matching uses the resolved exact attachment bytes, but output copies the original
manifest entries, **not** the embedded analysis view. All original metadata,
complete selected entries, `_file` reference spellings and empty `text`
placeholders remain intact. Retain the manifest plus every distinct selected
`request.postData._file` and `response.content._file` member exactly once, with
exact original uncompressed bytes and in source archive member order (including
the manifest's position); drop every other member. References in opaque extension
metadata are not interpreted. ZIP selection additionally reports
`retainedPayloadMembers` (excluding the manifest), including zero-byte members.
Embedded-only selections produce a manifest-only archive with count zero.
ZIP compression, timestamps, other ZIP metadata and manifest serialization may
change. Selection does not promise byte-identical ZIP output.

The entire archive is checked before selection, including unselected references
and unused members: invalid late content cannot be hidden by pruning. All archive
limits remain unchanged, including cumulative referenced bytes. Missing, unsafe,
ambiguous, case-aliased and manifest/self references are refused; no case-folded
reference fallback is used. JSON input still requires JSON output and embedded
bodies. Loose `_file`, trace `_sha1` and params-only bodies remain unsupported.
Capture filenames are never opened, members are never extracted, responses are
never synthesized and no HTTP requests are issued. No `--rules` flag is accepted.
Input/inventory aliases (including symlinks and hard links) are rejected.
Publication uses the existing exclusive atomic output writer, protecting both
inputs, refusing existing/dangling targets and cleaning temporary files on
failure. Local hard-link support is required; no power-failure durability claim.

## Explicit form embedding

HAR params contain decoded fields and cannot recover original body spelling
(for example `a+b` versus `a%20b`). To **choose new canonical bytes** for a
params-only URL-encoded fixture, run the opt-in `embed-forms` command first:

```sh
qawrench har embed-forms examples/har/form-params.har \
  --out /tmp/har-embedded-forms.har
qawrench har plan /tmp/har-embedded-forms.har \
  --rules examples/har/rules.json --inventory examples/har/form-requests.json \
  --out /tmp/har-forms-plan.json
qawrench har apply /tmp/har-embedded-forms.har \
  --rules examples/har/rules.json --inventory examples/har/form-requests.json \
  --out /tmp/har-forms-output.har
```

The synthetic input's `postData` is:

```json
{"mimeType":"application/x-www-form-urlencoded","params":[
  {"name":"tag","value":"a b"},
  {"name":"tag","value":"é+"},
  {"name":"empty","value":"","comment":"keep"}
]}
```

The new HAR adds `"text":"tag=a+b&tag=%C3%A9%2B&empty="` to this object.
The command reports `embeddedRequests: 1`; the subsequent plan has one matched
inventory request, one changed URL, and no collisions. Params, comments, URLs,
headers, response bytes and all other fields stay unchanged during embedding.
Existing embedded text/base64 is authoritative and is never rebuilt from params,
including noncanonical percent escapes and raw multipart bodies. Repeating
embedding on its own output is a no-op (`embeddedRequests: 0`).

The bounded serializer accepts `application/x-www-form-urlencoded` with no
charset or `charset=utf-8` (case-insensitive, optionally quoted). It follows
the [WHATWG form serializer](https://url.spec.whatwg.org/#urlencoded-serializing),
checked against [snapshot 845bafca](https://url.spec.whatwg.org/commit-snapshots/845bafca22fb0c5a42c4bf384a9bdea525a2f10d/#urlencoded-serializing):
UTF-8, spaces as `+`, uppercase percent escapes, literal alphanumerics and
`*-._`, ordered duplicate fields and explicit empty names/values. It performs no
newline or Unicode normalization. An empty params array produces empty text.
Only `mimeType`, `params`, optional `comment`, and param `name`/`value`/`comment`
are accepted for reconstruction. Each value must be present, even if empty.

File params, params-only multipart, unknown MIME parameters, non-UTF-8 charsets,
any declared payload encoding (including base64), and attached bodies fail.
Present request Content-Type headers must meet the same MIME restriction;
Content-Encoding must be absent or identity. Known nonnegative `bodySize` and
each Content-Length must equal the serialized byte length; sizes and headers
are never silently repaired. Unknown size `-1` or omitted size is allowed.
Any invalid entry anywhere in the HAR makes the command exit 2 **without an
output file**, even after earlier valid forms. Existing outputs and input
aliases are refused using the same atomic publication as apply.

Embedding selects a serializer; it does not prove the original request used
those bytes or certify collision safety. Review the chosen text and run
plan/preflight afterward. Ordinary plan/apply/preflight continue to reject
params-only bodies; inventories still require embedded text/base64.

## Multipart capture diagnostics

Embedded `multipart/form-data` requests are checked before plan, apply,
preflight, export-inventory, embed-forms, plan-selection or select-requests can
publish output. `postData.mimeType` must declare
exactly one boundary. Quoted values and MIME parameter names in any case are
supported; boundary values are case-sensitive. The boundary must have 1–70
ASCII characters from [RFC 2046 section 5.1.1](https://www.rfc-editor.org/rfc/rfc2046.html#section-5.1.1),
with no trailing space. Other well-formed MIME parameters are retained.
Every present request Content-Type must also declare multipart/form-data with
the same boundary; a header cannot supply a missing postData boundary.
Within each declaration, all parameter names must be unique case-insensitively,
even for identical values (`note=a; note=a`) or mixed case (`note=a; NOTE=b`).
Separate declarations may use the same names with different non-boundary values.
Duplicate non-boundary names report the request/declaration path and
`multipart MIME parameter has duplicate declarations; re-record with unique multipart MIME parameter names`,
without echoing names or values. Duplicate boundary declarations retain their
existing `multipart boundary has duplicate declarations` diagnostic.

Quoted declaration values may be empty or contain HTAB, printable ASCII and
valid UTF-8 characters. Double quotes and backslashes must be escaped with a
backslash; escapes accept printable ASCII (U+0020–U+007E) only. Quoted semicolons
remain value data. Literal C0 controls other than HTAB (U+0000–U+0008 and
U+000A–U+001F), and DEL (U+007F), are refused even in unknown parameters.
CR/LF retain the malformed-parameter diagnostic; other forbidden controls report
`multipart MIME quoted parameter contains forbidden control characters`.
Both identify the containing request and MIME field or indexed Content-Type
row and give re-record guidance, without echoing the parameter value. For example:

```text
log.entries[1].request.headers[2].value Content-Type: multipart MIME quoted parameter contains forbidden control characters; re-record with HTAB, printable or UTF-8 quoted values
```

This declaration check leaves boundary restrictions, part-header parsing,
response/non-multipart MIME handling and upload bytes unchanged. It never reads
`fileName` paths or interprets nested payloads.

Each multipart declaration is limited to **65,536 UTF-8 bytes and 128 MIME
parameters**, including `boundary`. The byte count includes the entire original
string: media type, whitespace, separators, quotes and escapes. Quoted semicolons
are value data, not extra parameters. The limits apply independently to
`postData.mimeType` and every request Content-Type value, including inventory
requests. Exactly 65,536 bytes and exactly 128 parameters are accepted and
preserved. The byte check precedes parameter regex processing; after 128
parameters, any remaining suffix is refused before it is parsed. Within these
limits, existing malformed-parameter and duplicate-boundary diagnostics apply.
Non-multipart declarations and response MIME types acquire no new size limit.

An excess declaration exits 2 before publication, identifying the containing
request and MIME field or zero-based header row, for example:

```text
log.entries[1].request.postData.mimeType: multipart MIME declaration exceeds 65536-byte UTF-8 limit; re-record with a smaller multipart MIME declaration
inventory.requests[1].headers[2].value Content-Type: multipart MIME parameter count exceeds 128-parameter limit; re-record with fewer multipart MIME parameters
```

A later violation blocks the whole command through JSON, ZIP resolution and
inventory guards, leaving no output or temporary file and no input mutation.
These metadata limits neither cap nor normalize upload bytes. Binary, text and
empty file payloads retain their existing exact-byte behavior.

The decoded text/base64 bytes must contain an opening delimiter and a later
closing delimiter at line starts, separated using CRLF. A boundary substring
inside file data is insufficient. Preamble, epilogue, space/tab delimiter
padding, and a closing delimiter at end-of-body without final CRLF are allowed.
This bounded subset requires at least one opening delimiter; a closing-only
empty multipart is rejected. For a valid synthetic binary upload:

```sh
qawrench har plan examples/har/multipart.har \
  --rules examples/har/rules.json --out /tmp/har-multipart-plan.json
qawrench har apply examples/har/multipart.har \
  --rules examples/har/rules.json --out /tmp/har-multipart-output.har
```

Both exit 0, reporting one entry, one changed URL and zero collisions. The
output changes the origin to `http://fixture.test`; the binary upload's base64
text, params and response stay unchanged. If its declared boundary is changed
to `wrong`, all commands exit 2 without publishing output, with:

```text
log.entries[0].request.postData: missing opening multipart boundary delimiter; re-record with exact embedded text/base64
```

A truncated closing delimiter reports `missing closing multipart boundary
delimiter`. Params-only uploads still fail, including with embed-forms. For
`params: [{"name":"file","fileName":"x.bin"}]` without `postData.text`, the
error identifies `postData.params[0].fileName: missing file content` and asks
for exact embedded text/base64. A filename, even with a param value, cannot
recover the captured upload. No file named by params is read from disk.

Each part must have a CRLF-delimited header block ending in a blank line.
Header names are ASCII MIME tokens immediately followed by `:`; values allow
HTAB, printable ASCII, and opaque bytes `0x80`–`0xff`, with optional surrounding
space/tab. Bare CR/LF, other controls, DEL and whitespace before `:` are rejected.
Names are case-insensitive. Folded continuation lines and duplicate header names
(including identical duplicates of other headers) are outside this subset and
are rejected rather than unfolded or combined.

Exactly one `Content-Disposition` must use the case-insensitive `form-data`
token and include one case-insensitive `name` parameter. An explicit empty
`name=""` is supported. Parameter values may be ASCII tokens or double-quoted
strings; quoted strings permit HTAB, printable/high bytes and backslash escapes
of printable ASCII. All duplicate parameter names are rejected case-insensitively.
Unknown parameters are allowed; `name*` does not supply `name`. Optional
`filename` and other headers are opaque metadata, never file paths to load or
instructions to decode file data. Nested multipart and part Content-Type or
Content-Transfer-Encoding semantics are not interpreted.

Each part's header block is limited to **65,536 bytes and 128 header lines**,
including `Content-Disposition`. Bytes include the CRLF between header lines,
but exclude the terminating CRLFCRLF and all payload/framing bytes. Exact limits
are accepted; exceeding either limit exits 2 before any output, with the request
path and zero-based part index (for example, `postData.parts[1]: part header
block exceeds 65536-byte limit` or `part header count exceeds 128-line limit`).
The byte limit is checked first, and both errors ask for a new recording with
smaller/fewer part headers. Separator search and header parsing are bounded by
these limits.

Each request is limited to **1,024 outer multipart parts**. Exactly 1,024 parts
are accepted. The next opening delimiter exits 2 before that part's headers are
processed, with the containing HAR/inventory request path and zero-based index:

```text
log.entries[1].request.postData.parts[1024]: part count exceeds 1024-part limit; re-record with fewer multipart parts per request
```

The count resets for each request. Closing delimiters and epilogues do not add
parts; boundary-like payload substrings and nested multipart delimiters remain
data. Binary and empty file payloads stay byte-for-byte unchanged. A later
violation blocks the whole command, including ZIP-resolved requests and inventory
guards, leaving inputs unchanged and no output or temporary file.

These fixed limits have no override flag and impose no new file-payload size
cap. They are not a whole-body or process-memory bound: JSON/base64 bodies still
load in memory and outer delimiter validation still scans payload bytes.

For a valid first file part followed by a part with only `Content-Type`, every
command exits 2 without output and reports, for example:

```text
log.entries[1].request.postData.parts[1]: missing Content-Disposition; re-record with Content-Disposition: form-data; name="field"
```

A `Content-Disposition: form-data; filename="x.bin"` instead reports
`missing name parameter in Content-Disposition; re-record with name="field"`.
`parts[1]` is the zero-based part index inside that request's raw body, not a
HAR params index or an added JSON field. Inventory errors use paths such as
`inventory.requests[1].postData.parts[1]`. Any later invalid part blocks the
whole command, including ZIP-resolved uploads. No partial output is published.

Valid embedded bytes remain authoritative. Empty files are accepted when
embedded in a framed body; missing param values do not imply missing bytes
when the raw body exists. These checks cover outer boundary syntax/framing, the bounded part-header
grammar above and absent embedded file content, not nested multipart or
agreement between params metadata and file contents. They follow the boundary
model of [RFC 7578 section 4.1](https://www.rfc-editor.org/rfc/rfc7578.html#section-4.1)
without parsing or reserializing file data. Multipart responses are unchanged.
Inventories receive the same request checks. Matching still compares exact
body bytes: uploads with different boundaries remain different request keys.

## Rule and plan semantics

- Origins match the **literal** `scheme://authority` boundary, including an
  explicitly spelled port and host case. No suffix, wildcard, regex, implicit
  default-port equivalence or path matching. Origins must omit trailing slashes,
  paths, query strings, fragments and credentials. Targets must be terminal:
  chained/cyclic mappings are rejected so reapplying rules is idempotent.
- `dropQueryKeys` names are case-sensitive, decoded UTF-8 names. `cache%42ust`
  matches `cacheBust`; `+` in a name decodes to space. All occurrences of named
  keys are removed, on **all origins**, including origins without mappings.
  Matching uses `&` separators and the first `=` in each token. Retained tokens
  are never re-encoded: their order, duplicates, values, escape spelling,
  valueless tokens, empty tokens and fragments survive. An all-removed query
  loses its `?`; an originally empty query is retained.
- Optional HAR `request.queryString` is decoded metadata. Rows whose literal
  `name` equals a dropped key are removed, with their indexes recorded in the
  plan. Other rows/extensions stay unchanged. The tool does not reconstruct,
  reconcile or reorder that metadata if the input disagrees with its raw URL.
- `dropRequestHeaders` is an optional array of nonempty ASCII HTTP header-name
  tokens: letters, digits, and ``! # $ % & ' * + - . ^ _ ` | ~``. Names match
  case-insensitively, without trimming or prefix/pattern matching. `*` is a
  literal token character. Whitespace, colons, controls, non-ASCII names,
  nonstrings and duplicate names (including case variants) are errors. All
  matching rows are removed from every `request.headers` array. The existing
  HAR validator permits opaque non-ASCII row names; they are never
  selected by ASCII rules, even Unicode case lookalikes such as `K` versus `K`.
  The original HAR is validated before filtering; selecting a malformed row cannot bypass
  validation, including inconsistent multipart Content-Type declarations.
- Header-removal reasons contain `kind: "dropRequestHeaders"`, original
  zero-based `removedIndexes`, and `removedHeaders`, a lowercase name/count
  array sorted by name. When the rule field is present, plan/apply summaries add
  `removedRequestHeaderRows` and `removedRequestHeaders` (the same sorted
  name/count format, aggregated over all requests). Empty rules/no matches
  report `0` and `[]`; omitting the field retains the previous summary shape.
  Header-only changes count in `changedEntries`. No removed header values or
  row extensions are copied into these new diagnostics.
- Unknown rule fields and duplicate JSON object keys are errors. Omitting
  `origins`, `dropQueryKeys` or `dropRequestHeaders` means an empty mapping/list;
  `{}` is a no-op. Case-insensitive duplicate checks apply only to header names;
  query-key names retain their case-sensitive behavior.
- Plans have `schemaVersion: 1`, a canonical input digest, all indexed
  `beforeURL`/`afterURL` pairs, reasons and removed raw query tokens, a summary,
  and `collisionGroups`. Each group key includes rewritten URL, exact method
  and exact body bytes (`bodyBase64`). Missing request bodies mean empty bytes;
  body whitespace is significant. Body grouping applies to all methods.
- Every duplicate key is reported, even if it existed before normalization.
  **Any difference in the entire response JSON object blocks apply**: status,
  headers (including order/case), content, cookies, redirects and even metadata
  differences. Object member order alone is ignored. Hashes are diagnostic;
  decisions compare canonical values, not hashes. This is deliberately more
  conservative than Playwright's request-header selection. Identical-response
  groups are allowed and all entries remain; nothing is merged or deleted.
- Apply recomputes validation and the plan from the supplied HAR/rules; it does
  not trust or execute an edited plan. Review the plan, then apply the same
  inputs. Deterministic outputs contain no timestamps or timings. A synced,
  same-directory temporary file is published with an atomic no-clobber hard
  link, then the temporary name is removed. No atomic guarantee is claimed for
  unusual/network filesystems or whole-machine power failure durability.

Exit status is `0` on success and `2` for validation, I/O, CLI usage, collision
or inventory-match errors. A blocked `plan` **still writes its diagnostics** and
exits `2`; a blocked `apply` creates no output. Other errors are actionable stderr
messages and do not create output. stdout contains one JSON summary on success
(and for written preflight/plan match diagnostics).

Plans may contain request URLs, raw query tokens and bodies; they are local
fixture artifacts, not redacted reports. Explicit header-row removal does not
redact copies of values in other fields. This tool performs no automatic
authentication cleanup. Header removal does not change the exact URL/method/body
key model or inventory requests; preflight does not check header equivalence.

## Supported HAR subset and boundaries

Compatibility is explicitly tested with authored HAR 1.2 fixtures based on
[Playwright v1.56.1's types](https://github.com/microsoft/playwright/blob/v1.56.1/packages/trace/src/har.ts)
and [content loading](https://github.com/microsoft/playwright/blob/v1.56.1/packages/playwright-core/src/server/harBackend.ts).
The optional interoperability check records a local-only attach ZIP with that exact
Playwright version and verifies offline `HarBackend` lookups. This is not a
claim of universal HAR exporter compatibility or full browser replay equivalence.

The validator requires `log.version == "1.2"`, a creator name/version, an entries
array, and request/response objects. Requests require a method token, absolute
HTTP(S) URL and name/value headers. Responses require an integer status 100–599,
name/value headers and embedded content with a MIME type. Present query metadata,
cookies, sizes and selected string fields are type-checked. Optional recording
metadata (pages, timing details and extensions) is preserved as opaque JSON;
this is not a full HAR schema validator. Reduced fixtures may omit nonessential
recording metadata. Methods are never case-normalized.

Payloads are UTF-8 text or strict base64. `postData` must include exact `text`
for plan/apply/preflight; params-only bodies require the explicit form embedding
step above to choose canonical bytes. Params may coexist
with text and are preserved. Nonempty known request `bodySize` without postData
is rejected. An absent request postData is otherwise treated as empty, not as an
inferred unknown body. Response content may omit text only with `size: 0`.
Loose `_file` or trace `_sha1` payload references are rejected even if text
also exists. Self-contained ZIP `_file` references use the adapter below.
Omitted nonempty response bodies, invalid base64, nonfinite
numbers, unpaired Unicode surrogates and unsupported versions are errors.

URLs use ASCII DNS/punycode or bracketed IPv6 hosts and optional numeric ports;
credentials, backslashes, whitespace/control characters and malformed percent
escapes are rejected. Query keys must decode as UTF-8; values are not decoded.
No DNS or network access occurs. Dropping a key is an explicit behavioral
assumption by the fixture maintainer. Header rows can only be removed by explicit
name, never edited. Cookies, redirects, response URLs, bodies and dynamic backend
behavior stay unchanged. Such fields can require separate fixture/application changes.

Playwright has multipart-boundary matching, request-header tie-breaking,
redirect handling and different treatment of bodies on non-POST methods. Those
behaviors are **not simulated** by this exact-key model. `safeToApply` means no
conflicting response and, when supplied, exactly one fixture per inventory
request within this model; it does not guarantee replay equivalence.
Inputs are loaded into memory; JSON inputs have no fixed memory cap. ZIP inputs
have the byte/member limits below, which bound input volume rather than total
Python process memory (JSON, base64 and copies add overhead).

## Self-contained Playwright HAR ZIPs

`plan`, `preflight`, `apply`, `export-inventory` and `select-requests` also accept
`.har.zip` input.
This is an adapter for the existing supported HAR subset, not a general ZIP or
trace importer. No additional flags or matching rules are added.

```sh
qawrench har plan recording.har.zip --rules rules.json --out plan.json
qawrench har apply recording.har.zip --rules rules.json --out repaired.har.zip
qawrench har preflight repaired.har.zip --inventory requests.json --out coverage.json
qawrench har export-inventory recording.har.zip --out requests.json
```

The input must contain **exactly one root-level, case-sensitive `.har` manifest**.
`request.postData._file` and `response.content._file` name exact archive-root
members, including safe subdirectories; no file is extracted or loaded from the
filesystem through a reference. The complete manifest and every archive member
are checked before any output. Attached bytes are represented as base64 only in
the internal embedded analysis view, so request keys, collision checks and
inventory gates use the exact bytes. Exported inventories embed those bytes as
base64 and remove `_file`; other postData metadata stays unchanged. Plan and
preflight digests identify the resolved embedded HAR, not the source ZIP bytes.

`apply` requires a fresh `.har.zip` output. It copies the original manifest and
changes only `request.url`, explicitly named `request.queryString` rows and
explicitly named `request.headers` rows.
Every non-HAR member, including unused members, retains its exact uncompressed
bytes, name and order. `_file` references and all other manifest fields stay
unchanged. ZIP compression, timestamps, metadata and manifest JSON serialization
may change. The source archive is never modified. JSON input still produces
JSON HAR and refuses a `.zip` output. `embed-forms` remains JSON-only; archive
requests must already contain exact embedded or attached bytes.

The strict archive contract rejects:

- Missing attachments, zero/multiple/nested HAR manifests, duplicate names and
  names ambiguous under Unicode case folding.
- Absolute/drive paths, backslashes, control characters, empty/`.`/`..` path
  segments, directories and nonregular members (including symlinks).
- Trace `_sha1`, encrypted ZIPs, compression other than stored/deflate, corrupt
  members/CRC, ambiguous payload sources and malformed JSON, including duplicate
  JSON keys. A `_file` cannot reference the manifest. An empty `text` placeholder
  alongside `_file` is accepted (emitted for POSTs by Playwright 1.56.1); nonempty
  `text` or any `encoding` alongside it is rejected.
- More than **64 MiB archive bytes, 4096 members, 16 MiB per uncompressed member
  (including the manifest), or 64 MiB total uncompressed bytes**. Declared sizes
  are checked before decompression; reads are bounded and CRC/actual lengths are
  checked. Cumulative referenced attachment bytes are also capped at 64 MiB,
  counting every reference, so repeated references cannot bypass the analysis
  volume limit. Output must satisfy the same byte limits. Limits have no override flag.

These errors exit 2 before publication, leaving no output or temporary file.
The existing same-directory atomic no-clobber publication policy also applies
to archives, input aliases, existing files, hard links and symlinks. It requires
a local filesystem supporting hard links. No loose external files are supported.
Multipart diagnostics, redirect behavior and header tie-breaking remain as
documented for the existing exact-key model.

The archive subset follows
[Playwright's attach format](https://playwright.dev/docs/api/class-page#page-route-from-har)
and its pinned
[v1.56.1 backend](https://github.com/microsoft/playwright/blob/v1.56.1/packages/playwright-core/src/server/harBackend.ts).
Compatibility was checked against archives recorded by Playwright 1.56.1 (GET JSON,
GET binary and attached binary POST) and offline `HarBackend.lookup` of rewritten
archives. That check does not cover browser routing, service workers, redirects or
multipart/header selection. Playwright is never a runtime dependency.

## Library

```python
from qawrench.har import build_plan, build_preflight, load_json, rewrite_har

source = load_json("examples/har/input.har")
rules = load_json("examples/har/rules.json")
plan = build_plan(source, rules)  # pure: neither argument changes
if plan["safeToApply"]:
    rewritten, verified_plan = rewrite_har(source, rules)  # copied result

inventory = load_json("examples/har/requests.json")
coverage = build_preflight(source, inventory, rules)  # omit rules for saved URLs
plan = build_plan(source, rules, inventory=inventory)
if plan["safeToApply"]:
    rewritten, verified_plan = rewrite_har(source, rules, inventory=inventory)
```

`DoctorError` is the actionable validation/collision exception.
`validate_har`, `validate_rules`, `validate_inventory` and `request_key` are also
exported; validate the containing HAR/inventory before using `request_key`
directly. The CLI owns atomic file publication.

`export_inventory(har)` returns a validated, independent inventory with the
first request for each exact key, or raises `DoctorError`. It follows the
export contracts above and does not modify its argument or access files.

`select_requests(har, inventory)` returns an independent HAR with complete
uniquely selected entries in source order and all document metadata, or raises
`DoctorError`. It reuses full preflight validation/matching without rewrites,
does not modify either argument and performs no filesystem/network access.

`embed_form_params(har)` returns a validated copied HAR with canonical text added
to supported params-only forms, or raises `DoctorError` without changing its
argument. It performs no filesystem access. It has the same explicit byte-choice
contract as `embed-forms`; ordinary `validate_har` remains strict about text.

## Tests and reproducible experiment

From a qawrench checkout:

```sh
uv run --extra test pytest tests/har -q
uv run python examples/har/benchmark.py
```

Tests run sequentially and exercise the CLI in subprocesses, GET/POST/text/base64 payloads,
query multiplicity, exact origins, no-op/idempotence, conflict refusal, malformed
inputs, no-overwrite behavior, injected disk failure and a racing output writer.
Inventory tests cover missing/unique/ambiguous IDs, identical-response ambiguity,
projected URLs, exact Unicode/text/base64 body matching, method case, malformed
inventories and apply refusal before publication.
Near-match regressions cover method/body/both differences, duplicate candidate
entries in HAR order, projected versus literal URLs, binary/Unicode/empty body
bytes, and empty suggestions for matched/ambiguous/unrelated requests.
Form tests cover ordered/duplicate/empty fields, Unicode and reserved bytes,
raw-body preservation, unsupported encodings/files, size/header conflicts,
whole-file failure and integration with plan/apply/preflight.
Multipart regressions cover case-insensitive duplicate declaration parameters,
quoted declaration controls and valid UTF-8/HTAB/escapes,
bounded MIME declaration bytes/parameter counts, boundary declarations, CRLF framing, truncated
captures, Content-Type disagreements, params-only file diagnostics and exact
binary/text/empty-file preservation.
Export regressions cover first-entry IDs/order, equivalent text/base64 and
empty bodies, binary bytes, exact URL/method distinctions, duplicate ambiguity,
whole-HAR validation and no-overwrite protection.
No scripts from HARs or rules are executed.

The benchmark generates exactly 1,000 synthetic entries: 900 distinct intended
keys plus 100 deliberate duplicates with identical responses. Intended target
keys are constructed independently of the rewrite function. It counts exact-key
matches before/after explicit origin/cacheBust normalization, times the work,
executes plan/apply end-to-end through temporary files and the standard JSON
parser, asserts every untouched field/payload, checks repeat apply, and injects
a response conflict to verify refusal. It leaves no persistent files.

See [examples/har/MEASUREMENTS.md](../examples/har/MEASUREMENTS.md) and the recorded JSON
for an actual local run. Counts are reproducible; timings vary. This measures
fixture match recovery and maintenance cost, **not replay speed**, and synthetic
results do not prove any real-site improvement. There is no promised speedup.

## License

The source and fixtures are MIT; see [LICENSE](../LICENSE) and
[THIRD_PARTY.md](../THIRD_PARTY.md).
