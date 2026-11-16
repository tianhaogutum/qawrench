# Synthetic offline fixtures for `qawrench har`

`selection-source.har` and `selection-requests.json` demonstrate inventory-driven
extraction: two of six entries survive, in capture order, despite reversed and
repeated selectors. The selected entries retain UTF-8 text, a binary multipart
GET body with opaque filename metadata, a binary response and all extensions.
Two unselected entries share a key with differing responses; selecting that
key would fail as ambiguous. Run `select-requests` then `preflight` as documented
in [docs/har.md](../../docs/har.md). No capture filename is opened and no host is contacted.

`input.har` is authored test data, not a browser capture. `fixtures.py` constructs
HAR 1.2 objects based on the pinned Playwright format references in
[docs/har.md](../../docs/har.md). It includes GET, POST with exact Unicode JSON text, a binary/base64
response, duplicate query keys and a similar suffix host that must not map.
`rules.json` contains the explicit example policy. No host is contacted.
`requests.json` is an independently spelled target request inventory: preflight
with the rules matches each of its four requests to exactly one saved entry.
Without the rules, the first three requests are missing and the suffix-host
request still matches. See [docs/har.md](../../docs/har.md) for preflight and guarded apply.
`near-match-requests.json` deliberately misses all four requests: after applying
the rules, the first three have `/log/entries/1` as a diagnostic candidate with
method-only, body-only, and both differences. The fourth URL has no candidate.
Preflight/plan exit 2 and guarded apply publishes no HAR; suggestions never
relax exact matching.

`multipart.har` contains an embedded base64 multipart file upload with binary
bytes `00 ff` and boundary `upload:42`. Plan/apply with `rules.json` change only
the origin; the upload stays byte-exact. Changing the declared boundary or
truncating the closing delimiter fails before output publication. Params-only
file metadata cannot reconstruct an upload; see [docs/har.md](../../docs/har.md) diagnostics.

Run `uv run python examples/har/benchmark.py` from the qawrench checkout root to generate the
1,000-entry comparison in memory and exercise both CLI operations on temporary
files. `benchmark-results.json` is one recorded run; `MEASUREMENTS.md` explains
its environment and scope. All example Python files are test/benchmark programs
owned by this project; HAR/rules data is never evaluated as code.

Inventory export: `export-source.har` is synthetic and contains 9 entries with
7 distinct exact URL/method/body keys. Text and base64 for the same bytes and
missing/explicit empty bodies are duplicate keys. It also includes method case,
GET bodies, raw URL spelling and binary bytes. Run `export-inventory` then
`preflight` as documented in [docs/har.md](../../docs/har.md); export succeeds, while preflight
exits 2 with 5 matched and 2 ambiguous requests. No URLs or bodies are inferred.

`request-headers.har` and `request-header-rules.json` demonstrate explicit
case-insensitive header-row removal: four matching rows across two requests,
including duplicate X-Request-ID rows with different casing, and a third request
with no matches. Plan/apply report `authorization: 2` and `x-request-id: 2` without
header values. Text/base64 bodies, Cookie/header-prefix rows, URL/query/cookie
metadata and response headers/cookies stay unchanged. Only named request-header
rows are removed; this is not a comprehensive secret scrubber. See
[docs/har.md](../../docs/har.md) for commands and matching limitations.
