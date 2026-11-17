# Third-party sources and attribution

qawrench is licensed under the MIT License (see [LICENSE](LICENSE)). Its commands were
previously separate MIT-licensed projects written by the same author and are merged
here under the same license. Unless a section below says otherwise, all code,
synthetic fixtures, tests and documentation are original work; they contain no
production captures, customer data, credentials or real-site recordings.

qawrench is an independent interoperability project. It is not affiliated with or
endorsed by Microsoft (Playwright), Google (Lighthouse, Chrome), Deque Systems (axe),
the Pa11y project, Apify (Crawlee), the Apache Software Foundation (Arrow, Parquet),
the lycheeverse maintainers (Lychee), OASIS (SARIF), Scrapinghub/Zyte (extruct, w3lib),
the pytest, Mock Service Worker (MSW), OpenAPI Initiative or Mermaid projects, or any other
project named below. Product names only identify interoperable file
formats. This file is attribution; it does not relicense any dependency or upstream
material.

The standalone projects kept their original research notes for provenance. Those
notes are not part of this repository and are not needed to build, test or run
qawrench.

## Dependencies (installed separately, never vendored)

The base install has no third-party runtime dependencies. Heavier dependencies are
optional extras, installed by your package manager under their own licenses; they
keep their own notices in their distributions. The qawrench wheel contains only the
`qawrench` package and these project notices.

| Package | License | Used by | Kind |
| --- | --- | --- | --- |
| [PyArrow](https://github.com/apache/arrow) | Apache-2.0 (wheel bundles further notices) | `crawlee-export` | extra `parquet` |
| [extruct](https://github.com/scrapinghub/extruct) | BSD-3-Clause | `structured-data` | extra `structured` |
| [lxml](https://lxml.de/) | BSD-3-Clause plus component notices | `structured-data` | extra `structured` |
| [beautifulsoup4](https://www.crummy.com/software/BeautifulSoup/) | MIT | `page-diff` | extra `html` |
| [soupsieve](https://github.com/facelessuser/soupsieve) | MIT | `page-diff` | extra `html` |
| [w3lib](https://github.com/scrapy/w3lib) | BSD-3-Clause | `frontier` | extra `frontier` |
| [PyYAML](https://github.com/yaml/pyyaml) | MIT | `msw-contract` | extra `openapi` |
| [jsonschema](https://github.com/python-jsonschema/jsonschema) (with referencing, attrs, jsonschema-specifications, rpds-py) | MIT | `msw-contract` response validation; `a11y` SARIF schema tests | extra `openapi`; test |
| [DeepDiff](https://github.com/seperman/deepdiff) | MIT (requires orderly-set, MIT) | `statewatch` pytest plugin | extra `statewatch` |
| [pytest-replay](https://github.com/ESSS/pytest-replay) | MIT | `statewatch` confirmation replays | extra `statewatch` |
| [filelock](https://github.com/tox-dev/filelock) | Unlicense | `statewatch` output lock | extra `statewatch` |
| [pytest](https://github.com/pytest-dev/pytest) | MIT | test runner; `statewatch` plugin host | test; extra `statewatch` |
| [rfc3986-validator](https://github.com/naimetti/rfc3986-validator) | MIT | `uri` format checks in `a11y` tests | test |
| [Hatchling](https://github.com/pypa/hatch) | MIT | build backend | build |
| [hatch-fancy-pypi-readme](https://github.com/hynek/hatch-fancy-pypi-readme) | MIT | PyPI README link rewriting | build |

Python and its standard library (including `sqlite3`, `zipfile` and `zlib`) are
provided by the user's Python installation under the
[PSF License Agreement and bundled notices](https://docs.python.org/3/license.html).
SQLite is [public domain](https://www.sqlite.org/copyright.html). No SQLite or zlib
source is vendored.

`msw-contract` also needs [Node.js](https://nodejs.org/) 24 or newer (MIT, with bundled
third-party notices) on the user's `PATH` and the [MSW](https://github.com/mswjs/msw)
package (MIT) installed in the user's own project. qawrench ships neither; it only
ships the small Node runner script that calls the user's MSW install (see the
`msw-contract` section).

### License texts in `licenses/`

[`licenses/`](licenses) keeps the license and notice files the standalone projects
recorded for the dependency versions they were tested with (copied unchanged from the
installed distributions), and the licenses of upstream files carried as test fixtures
or examples. They document those dependencies; the files are not shipped
inside the wheel, and none of these packages is vendored.

| Directory | Contents |
| --- | --- |
| [`licenses/crawlee-export/`](licenses/crawlee-export) | PyArrow LICENSE and NOTICE, Crawlee LICENSE |
| [`licenses/frontier/`](licenses/frontier) | w3lib 2.3.1 |
| [`licenses/page-diff/`](licenses/page-diff) | beautifulsoup4 4.14.3, soupsieve 2.8.1, typing_extensions 4.15.0 |
| [`licenses/structured-data/`](licenses/structured-data) | extruct 0.18.0, lxml 6.1.3 and the rest of extruct's tested dependency closure |
| [`licenses/statewatch/`](licenses/statewatch) | pytest-leak-finder LICENSE (MIT), for the demo copied into `tests/statewatch/fixtures/` |
| [`licenses/msw-contract/`](licenses/msw-contract) | Apache-2.0 licenses of the OpenAPI Initiative and openapi-generator repositories, for the Petstore documents in `tests/msw_contract/fixtures/upstream/` and `examples/msw-contract/`, with their `SOURCES.md` |
| [`licenses/mermaid-paths/`](licenses/mermaid-paths) | Mermaid LICENSE (MIT), for the flowchart examples in `tests/mermaid_paths/fixtures/upstream/` |

## flaky (formerly retry-history-ledger)

Original implementation and synthetic examples (`examples/flaky/`, `tests/flaky/fixtures/`):
MIT.

**Runtime:** no third-party Python packages. Uses only the Python standard library,
including `sqlite3` (Python: PSF License Agreement; SQLite: public domain), both supplied
by the user's runtime and not bundled.

**Tests:** standard library `unittest` only (run under pytest); no extra test packages.

**Format interoperability (not dependencies):**

| Upstream | Inspected version | License | Use |
| --- | --- | --- | --- |
| [microsoft/playwright](https://github.com/microsoft/playwright/tree/v1.56.1) | v1.56.1 | [Apache-2.0](https://github.com/microsoft/playwright/blob/v1.56.1/LICENSE) | Native JSON reporter field/enum semantics |
| [ctrf-io/playwright-ctrf-json-reporter](https://github.com/ctrf-io/playwright-ctrf-json-reporter/tree/v0.0.29) | v0.0.29, commit `13d31455f607221d1db5aca758f359d648a989f9` | [MIT](https://github.com/ctrf-io/playwright-ctrf-json-reporter/blob/v0.0.29/LICENSE) | Comprehensive legacy CTRF fields, retry/status mappings |

No upstream implementation code, node modules, browser binaries or upstream fixtures are
copied. Upstream source was inspected to design original adapters; all fixtures are
hand-authored synthetic data. JUnit XML output follows the common JUnit XML convention;
no schema text is copied.

Playwright is a trademark of Microsoft Corporation; CTRF is a project of ctrf-io. qawrench
is not affiliated with or endorsed by either.

## har (formerly har-replay-doctor)

The implementation and the synthetic fixtures in `examples/har/` and `tests/har/` are
original work under the qawrench MIT license. No Playwright implementation source,
fixture, browser binary or real-site HAR data is copied or bundled.

**Runtime and test dependencies.** None beyond the Python standard library (Python is
covered by the PSF License Agreement, <https://docs.python.org/3/license.html>). The
tests use only `unittest` from the standard library.

**Formats and specifications referenced (no copied text or code):**

| Upstream | License | Use |
| --- | --- | --- |
| [microsoft/playwright](https://github.com/microsoft/playwright/tree/v1.56.1) | Apache-2.0 | HAR 1.2 shape, `.har.zip` attach format and replay-matching reference; no copying, no runtime dependency |
| [WHATWG URL Standard, form serializer](https://url.spec.whatwg.org/#urlencoded-serializing) (snapshot [845bafca](https://url.spec.whatwg.org/commit-snapshots/845bafca22fb0c5a42c4bf384a9bdea525a2f10d/#urlencoded-serializing)) | CC BY 4.0 (standard text) | `embed-forms` serialization behavior reference only |
| [RFC 2046 section 5.1.1](https://www.rfc-editor.org/rfc/rfc2046.html#section-5.1.1), [RFC 7578 section 4.1](https://www.rfc-editor.org/rfc/rfc7578.html#section-4.1) | IETF Trust | Multipart boundary syntax and framing reference only |

Playwright sources inspected: [HAR replay docs](https://playwright.dev/docs/mock#replaying-from-har),
[v1.56.1 HAR types](https://github.com/microsoft/playwright/blob/v1.56.1/packages/trace/src/har.ts),
[replay backend](https://github.com/microsoft/playwright/blob/v1.56.1/packages/playwright-core/src/server/harBackend.ts)
and [HAR tracer](https://github.com/microsoft/playwright/blob/v1.56.1/packages/playwright-core/src/server/har/harTracer.ts).
The pinned revision is the inspected compatibility baseline, not a claim that all HARs
from that or later versions are supported. An optional interoperability check of the
original tool installed `playwright-core@1.56.1` (Apache-2.0) outside the repository
against an authored loopback server; nothing from it is distributed.

**Trademarks.** Playwright is a project of Microsoft. qawrench is not affiliated with or
endorsed by Microsoft or the Playwright project; the name is used only to describe
compatibility.

## trace-budget (formerly trace-size-budget)

The `qawrench.trace_budget` implementation, its tests and the synthetic trace
generator/benchmark in `examples/trace_budget/` are original work under qawrench's
MIT license. No Playwright implementation source, browser, recording or asset is
copied or bundled; all test archives are generated synthetically.

**Runtime and test dependencies:** none outside the Python standard library
(`zipfile`, `zlib`, `json`, `hashlib`, ...). Python (PSF-2.0 and its accompanying
notices, [license](https://docs.python.org/3/license.html)) and zlib
([Zlib license](https://www.zlib.net/zlib_license.html); tested with zlib 1.3.1) are
supplied by the user's interpreter/system and are not bundled.

**Format references (not dependencies):**

| Reference | Use | License |
|---|---|---|
| [Microsoft Playwright v1.56.1](https://github.com/microsoft/playwright/tree/v1.56.1) trace types (`packages/trace/src/trace.ts`, `har.ts`) and recorder (`tracing.ts`) | Version-8 trace ZIP/NDJSON envelopes and resource-reference keys; the implementation is independently written | Apache-2.0 ([license](https://github.com/microsoft/playwright/blob/v1.56.1/LICENSE)) |
| [PKWARE APPNOTE 6.3.10](https://pkware.cachefly.net/webdocs/casestudies/APPNOTE.TXT) | ZIP local/central header, data descriptor, ZIP64 and end-record consistency checks | Specification, referenced only |

**Trademarks:** Playwright is a project of Microsoft; PKWARE and APPNOTE are
associated with PKWARE, Inc. The names identify the formats read; qawrench is not
affiliated with or endorsed by Microsoft or PKWARE.

## a11y (formerly a11y-baseline-bridge)

The `qawrench.a11y` implementation, its two constrained SARIF output schemas
(`src/qawrench/a11y/schemas/*.schema.json`) and the hand-authored synthetic fixtures
in `examples/a11y/` are original work under qawrench's MIT license. No axe-core,
Pa11y CI, Pa11y, HTML_CodeSniffer or converter source is copied, bundled, imported or
installed. Interoperating with their report formats does not relicense those projects.

**Runtime dependencies:** none (Python standard library only; URI escaping uses the
standard library).

**Test-only dependencies (not vendored):**

| Distribution | License | Purpose |
|---|---|---|
| jsonschema | MIT | Offline structural validation of SARIF output against the shipped schemas |
| rfc3986-validator | MIT | Registers jsonschema's `uri` format checker for those tests |
| attrs, jsonschema-specifications, referencing, rpds-py | MIT | Transitive dependencies of jsonschema |
| typing-extensions | PSF-2.0 | Conditional transitive dependency on older Pythons |

**Format references (not dependencies; read only to shape adapters and fixtures):**

| Project / inspected version | Upstream license | Use |
|---|---|---|
| [dequelabs/axe-core 4.10.3](https://github.com/dequelabs/axe-core/tree/v4.10.3) | MPL-2.0 | API/type documentation for native results, targets and `getRules` metadata |
| [dequelabs/axe-core 4.11.0](https://github.com/dequelabs/axe-core/tree/v4.11.0) | MPL-2.0 | Full-v1/`getRules` formats for migration diagnostics; custom-rule synthetic fixtures |
| [pa11y/pa11y-ci 4.0.0](https://github.com/pa11y/pa11y-ci/tree/4.0.0) | LGPL-3.0-only | Combined report format and serialization behavior |
| [pa11y/pa11y 9.0.0](https://github.com/pa11y/pa11y/tree/9.0.0) | LGPL-3.0-only | Issue/`runnerExtras` format inspection |

SARIF output links rule help to upstream documentation URLs (axe-core docs,
HTML_CodeSniffer, Pa11y's axe runner); no upstream content is embedded.

**SARIF:** SARIF 2.1.0 is an OASIS standard; qawrench claims no ownership of the
format. The shipped schemas are original, constrained output contracts, not the
official full SARIF schema (`https://json.schemastore.org/sarif-2.1.0.json`), which
is neither redistributed nor fetched by tests.

**Trademarks:** axe and axe-core are associated with Deque Systems; Pa11y and
HTML_CodeSniffer are names of their respective projects. These names are used only to
identify the report formats read; qawrench is not affiliated with or endorsed by them.

## lighthouse (formerly lighthouse-noise-gate)

Original implementation and synthetic fixtures (`examples/lighthouse/`): MIT.

**Runtime and tests:** no dependencies beyond the Python standard library (CPython is
separately installed under the PSF License Agreement and is not bundled).

**Format interoperability only (not dependencies):**

| Project | Inspected release | License | Use |
| --- | --- | --- | --- |
| [GoogleChrome/lighthouse](https://github.com/GoogleChrome/lighthouse/tree/v12.8.2) | 12.8.2 | [Apache-2.0](https://github.com/GoogleChrome/lighthouse/blob/v12.8.2/LICENSE) | LHR JSON field names, units, navigation/config contract |
| [GoogleChrome/lighthouse-ci](https://github.com/GoogleChrome/lighthouse-ci/tree/v0.15.1) | 0.15.1 docs | [Apache-2.0](https://github.com/GoogleChrome/lighthouse-ci/blob/v0.15.1/LICENSE) | Existing collection and assertion aggregation behavior (referenced for comparison) |

No upstream implementation, measured report data, executable or npm dependency is
copied or distributed. The synthetic fixtures are independently authored projections
of the documented LHR format, not upstream sample data.

Lighthouse and Chrome are trademarks of Google LLC; qawrench is not affiliated with or
endorsed by Google.

## page-diff (formerly stable-page-diff)

The comparator, fixtures, tests and documentation are original MIT-licensed work.
Dependencies are installed separately (via the `html` extra) and keep their own
licenses; no dependency code is copied or vendored into `qawrench.page_diff`. The
tests use only the Python standard library (unittest) plus the runtime
dependencies (`examples/page_diff/experiment.py` also uses BeautifulSoup).

| Distribution | Role | License |
| --- | --- | --- |
| [beautifulsoup4](https://www.crummy.com/software/BeautifulSoup/) (tested 4.14.3) | Runtime HTML parser interface (`html.parser` backend) | MIT |
| [soupsieve](https://github.com/facelessuser/soupsieve) (tested 2.8.1) | Runtime CSS selectors; also a BeautifulSoup dependency | MIT |
| [typing_extensions](https://github.com/python/typing_extensions) (tested 4.15.0) | BeautifulSoup runtime dependency | PSF-2.0, with the accompanying historical Python license notices |

Notes:

- [changedetection.io](https://github.com/dgtlmoon/changedetection.io) (Apache-2.0)
  and its [issue 2469](https://github.com/dgtlmoon/changedetection.io/issues/2469)
  are referenced only as comparison context. No changedetection.io code is
  imported, copied, vendored or required at runtime, and no compatibility with its
  configuration is claimed.
- All example and test HTML is synthetic and hand-authored for this tool; it is not
  captured from any real site.
- BeautifulSoup, Soup Sieve and changedetection.io names are used descriptively;
  qawrench is not affiliated with or endorsed by their authors.

## structured-data (formerly structured-data-parity)

Original code is MIT. No upstream extractor implementation is copied into
`qawrench.structured_data`; dependencies are installed separately (via the
`structured` extra) and keep their own licenses. The tests use only the Python
standard library (unittest) plus the runtime dependencies.

Direct runtime dependencies:

| Distribution | License | Use |
| --- | --- | --- |
| [extruct](https://github.com/scrapinghub/extruct) (tested 0.18.0) | BSD-3-Clause, copyright Scrapinghub | Actual JSON-LD / OpenGraph extraction |
| [lxml](https://lxml.de/) (tested 6.1.3) | BSD-3-Clause with additional component notices (see below) | HTML parsing and source line locations |

Transitive dependencies pulled in by extruct (tested closure): lxml_html_clean,
w3lib, webencodings, idna, rdflib (BSD-3-Clause); html-text, jstyleson, mf2py,
beautifulsoup4, soupsieve, html5lib, six, pyparsing, charset-normalizer, urllib3
(MIT); frostwork, requests (Apache-2.0; requests ships a NOTICE); typing_extensions
(PSF-2.0); pyrdfa3 (W3C Software Notice and License); certifi (MPL-2.0, CA bundle
installed unchanged). Microformat and RDFa extraction are not invoked, and no
runtime network request is made.

Notes:

- **lxml is not uniformly covered by one license.** Its `LICENSES.txt` describes
  ElementTree- and CPython-derived code, PSF notices, BSD code, GPL-licensed
  upstream test-runner material and Schematron resources (not used here), and for
  binary wheels zlib (zlib license), iconv (LGPL-2.1), libxml2, libxslt and libexslt
  (MIT). The qawrench wheel does not contain lxml or these libraries; redistributors
  bundling dependency binaries must retain their component notices and any
  source/relinking obligations.
- **jstyleson 0.0.2** ships no license text in its release; its `PKG-INFO` declares
  MIT (author linjackson). The upstream
  [LICENSE](https://github.com/linjackson78/jstyleson/blob/master/LICENSE)
  (copyright 2016 linjackson) is from the master branch, not a verified release tag.
- **pyrdfa3** carries the W3C copyright, permission and disclaimer text in its
  release license; no SPDX revision is substituted for it.
- Upstream behavior and formats referenced: extruct's JSON-LD and OpenGraph
  extraction output, [JSON-LD](https://www.w3.org/TR/json-ld11/), the
  [Open Graph protocol](https://ogp.me/) and JSON Pointer (RFC 6901) for report paths.
- All example and test HTML is synthetic and was written for this tool; no real
  site captures are included. No dependency source was modified.
- extruct, lxml, schema.org and Open Graph names are used descriptively; qawrench is
  not affiliated with or endorsed by their authors.

### Inspected extraction contract (from the original UPSTREAM.md)

Inspected 2026-11-11: the installed **extruct 0.18.0** distribution (PyPI) and its
[upstream README](https://github.com/scrapinghub/extruct/blob/master/README.rst). The
installed release source, rather than assumptions about the current master branch,
determines this command's compatibility contract.

- [`extruct/_extruct.py`](https://github.com/scrapinghub/extruct/blob/v0.18.0/extruct/_extruct.py):
  `extract` accepts a parsed lxml HTML tree, `base_url`, `syntaxes`, `errors`,
  `uniform`. `qawrench structured-data` selects only JSON-LD and OpenGraph and uses
  strict errors and nonuniform output. The RDFa, microformat, microdata and Dublin
  Core extractors are never invoked.
- [`extruct/jsonld.py`](https://github.com/scrapinghub/extruct/blob/v0.18.0/extruct/jsonld.py):
  selects scripts with the exact type `application/ld+json` and returns dictionaries
  and list members. Its permissive decoder tries jstyleson, and its flattening filters
  falsy items. It does not expand JSON-LD or resolve relative `@id`s. The strict
  preflight checks every selected script, records provenance, and verifies the
  extracted roots against validated JSON before comparison.
- [`extruct/opengraph.py`](https://github.com/scrapinghub/extruct/blob/v0.18.0/extruct/opengraph.py):
  returns objects with `namespace` and `properties` (a list of `(property, value)`
  pairs), reading direct head meta children with property/content and the built-in
  OG-family namespaces or declared prefixes. Repeated values and the namespace map
  are preserved; uniform output, which changes this shape, is not used. An
  independent source-selection preflight implements that release's prefix regex,
  built-in namespaces and direct head/meta rule, then verifies the complete returned
  block list, so silent empty or partial OpenGraph extraction is an error.
- lxml **6.1.3** parses UTF-8 decoded text with a non-network HTML parser. Source line
  numbers are used for diagnostics, not as a claim of exact original byte offsets
  after HTML parser recovery.

Scope deliberately narrows upstream: strict standard JSON, identified entities, and
top-level graph wrappers containing only `@context`/`@graph`. Unsupported values fail
with provenance. Contexts remain plain JSON and are never fetched. See
[docs/structured-data.md](docs/structured-data.md) for the complete comparison policy.

## links (formerly lychee-sarif-map)

The adapter code and the synthetic documents, manifests and reports in `examples/links/`
and `tests/links/fixtures/` are original work under the qawrench MIT license, except
the historical report noted below. The command does not bundle, link or execute Lychee.

**Runtime and test dependencies.** None beyond the Python standard library (Python is
covered by the PSF License Agreement, <https://docs.python.org/3/license.html>). The
tests use only `unittest` and other standard-library modules. The original project
optionally validated output against the official SARIF schema with
[jsonschema](https://github.com/python-jsonschema/jsonschema) (MIT, with its MIT
dependencies attrs, jsonschema-specifications, referencing and rpds-py); that check is
not part of qawrench's runtime or test suite.

**Formats and specifications referenced:**

- [lycheeverse/lychee](https://github.com/lycheeverse/lychee) `lychee-v0.18.1` JSON
  serializers, Apache-2.0
  ([LICENSE-APACHE](https://raw.githubusercontent.com/lycheeverse/lychee/lychee-v0.18.1/LICENSE-APACHE)).
  Used only to understand the JSON report format; no Rust source is copied, linked,
  executed or redistributed.
- [OASIS SARIF 2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/) and its
  [schema](https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema/sarif-schema-2.1.0.json),
  governed by the [OASIS terms](https://github.com/oasis-tcs/sarif-spec/blob/main/LICENSE.md).
  Output-format reference only; the schema is not distributed.

**Copied upstream material.** `tests/links/fixtures/issue1637.json` transcribes the factual
JSON report posted by dveeden in
[lychee issue #1637](https://github.com/lycheeverse/lychee/issues/1637) (2025-02-18). It
is attributed historical format evidence, not output generated by qawrench; the issue
author's content is not claimed to be relicensed under MIT. The issue's original source
document is not included.

**Trademarks.** Lychee is a project of the lycheeverse maintainers; SARIF is an OASIS
standard. qawrench is not affiliated with or endorsed by either; the names are used only
to describe format compatibility.

## frontier (formerly crawl-frontier-audit)

The audit code, the `verify-witnesses` subcommand (formerly the separate
`verify-robots-witnesses` script) and all synthetic examples in `examples/frontier/`
are original work under the project's MIT license. No upstream code is vendored or
copied.

| Component | Role | License |
| --- | --- | --- |
| [w3lib](https://github.com/scrapy/w3lib) | Runtime dependency of this command, installed via the `frontier` extra (tested with 2.3.1; no transitive dependencies). Its real `canonicalize_url` is called only for explicitly enabled query sorting | BSD-3-Clause (wheel metadata says `BSD`; the license text is the three-clause form) |
| [Crawlee](https://github.com/apify/crawlee) (JavaScript) | A narrow documented JSON `RequestList` sources / `RequestOptions` subset is read as an input format; Crawlee is not installed, embedded, executed or copied, and no Node runtime is needed | Apache-2.0 |
| Python standard library | Runtime and tests (`unittest` only) | PSF License |

Specifications and formats referenced:

- [RFC 9309](https://www.rfc-editor.org/rfc/rfc9309.html) (Robots Exclusion
  Protocol) for offline robots.txt matching of locally supplied snapshots; some
  tests exercise the precedence examples from Google's public robots.txt
  documentation, re-expressed as synthetic fixtures (no text copied).
- w3lib's `canonicalize_url` behaviour (v2.3.1) and Crawlee's documented
  `RequestList` / `RequestOptions` shapes, as described in `docs/frontier/upstream-support.md`.

The policy, checkpoint, robots-manifest and report formats (all `version` /
`schema_version` 1, checkpoints 1/2) are defined by this project.

Crawlee and Apify are trademarks of their respective owners; Scrapy/w3lib names
belong to their maintainers; Google is a trademark of Google LLC. This project is
not affiliated with or endorsed by any of them.

## crawlee-export (formerly crawlee-parquet-export)

The exporter code and the synthetic fixtures in `examples/crawlee_export/` are
original work under the project's MIT license. No upstream code has been copied
into the implementation.

| Component | Role | License |
| --- | --- | --- |
| [Apache Arrow / PyArrow](https://github.com/apache/arrow) | Runtime dependency of this command, installed via the `parquet` extra (tested with 23.0.1) | Apache-2.0 |
| [Crawlee](https://github.com/apify/crawlee) (JavaScript, v3.15.3) | Local memory-storage dataset layout (numbered item files, `__metadata__.json`) inspected from source only; not installed, linked or vendored | Apache-2.0 |
| Python standard library | Runtime and tests | PSF License |

PyArrow's binary wheels include native libraries and bundled third-party components
with their own permissive notices (Apache-2.0, MIT, BSD, Boost-1.0 and others); the
complete LICENSE and NOTICE files shipped inside the installed PyArrow wheel are
authoritative and must be retained when redistributing that wheel. No NumPy, pandas,
Spark, Crawlee Node runtime or browser is required or bundled.

Formats referenced: Apache Parquet (written via PyArrow as format version 2.6) and
JSON Lines. The tool's own schema format, manifest sidecar (`format_version` 1) and
resume checkpoint (`resume_version` 2) are defined by this project.

Crawlee and Apify are trademarks of their respective owners; Apache Arrow and Apache
Parquet are trademarks of the Apache Software Foundation. This project is not
affiliated with or endorsed by Apify, the Crawlee project or the ASF.

## statewatch (formerly pytest-statewatch)

The pytest plugin, the `qawrench statewatch` report reader, the `statewatch` check of
`qawrench check`, the tests in `tests/statewatch/` and the authored demo in
`examples/statewatch/` (`test_service.py`, `policy.json` and the synthetic
`report.json`) are original work under the qawrench MIT license, except the upstream
material listed below. The standalone project was released under the MIT License
(Copyright (c) 2026 tianhaogutum).

**Runtime dependencies (extra `statewatch`, installed separately, never vendored).**
Only the pytest plugin needs them, and it imports them only after
`--statewatch POLICY.json` is given; the report reader and the `check` integration use
the Python standard library only.

| Package | Range | Role | License |
| --- | --- | --- | --- |
| [pytest](https://github.com/pytest-dev/pytest) | `>=8.2,<10` | Plugin host: collection, fixtures, test phases and outcomes | MIT |
| [DeepDiff](https://github.com/seperman/deepdiff) | `>=8.6,<9` | Structured before/after diffs (`zip_ordered_iterables=True`); pulls in orderly-set (MIT) | MIT |
| [pytest-replay](https://github.com/ESSS/pytest-replay) | `>=1.7,<2` | Replay input format (`ReplayTestInfo`) and ordered execution of confirmation runs | MIT |
| [filelock](https://github.com/tox-dev/filelock) | `>=3.16,<4` | Exclusive lock on the output directory | Unlicense |

The original project's tested minimums were Python 3.11 with pytest 8.2.0,
pytest-replay 1.7.0, DeepDiff 8.6.0 and filelock 3.16.0; newer combinations used
pytest 8.4.2 and 9.1.1 with pytest-replay 1.8.0, DeepDiff 8.6.1 and filelock 3.20.0, and
its final wheel check used pytest 9.2, DeepDiff 8.6.2 and filelock 3.32.7. Python
itself is covered by the PSF License Agreement (<https://docs.python.org/3/license.html>).

**Optional test composition (not dependencies).** Some tests run only when these
plugins happen to be installed and are skipped otherwise:
[pytest-rerunfailures](https://github.com/pytest-dev/pytest-rerunfailures) (MPL-2.0),
[pytest-randomly](https://github.com/pytest-dev/pytest-randomly) (MIT) and
[pytest-xdist](https://github.com/pytest-dev/pytest-xdist) (MIT). None is installed by
qawrench.

**Copied upstream material.**

- `tests/statewatch/fixtures/leak_finder_demo.py` is the unmodified
  [`demo/test_demo.py`](https://raw.githubusercontent.com/mgaitan/pytest-leak-finder/master/demo/test_demo.py)
  of [pytest-leak-finder](https://github.com/mgaitan/pytest-leak-finder) (downloaded
  2026-11-11; only the file name changed). MIT License, Copyright (c) 2022 Martín
  Gaitán; the license text is kept in
  [`licenses/statewatch/pytest-leak-finder-LICENSE.txt`](licenses/statewatch/pytest-leak-finder-LICENSE.txt).
  It is not part of the wheel.
- `tests/statewatch/fixtures/lanes-upstream-report.json` is unmodified JSON output of
  [pytest-threadlanes](https://github.com/Heknon/pytest-threadlanes) 0.1.0, and
  `doctor-order-report.json` and `doctor-upstream-report.json` are unmodified JSON
  output of [pytest-flakedoctor](https://github.com/Therealdk8890/pytest-flakedoctor)
  0.1.2, each produced by running that tool on the pytest-leak-finder demo above. They
  are kept as factual format evidence for the `--from-lanes` and `--from-doctor`
  adapters; no code of either tool is copied, installed or executed by qawrench.

**Formats and prior art referenced (no code copied).** The pytest-threadlanes 0.1.0
shared-state report and the flakedoctor v1 report (`format: flakedoctor-report`) are
read as input formats only. [detect-test-pollution](https://github.com/asottile/detect-test-pollution),
[pytest-find-dependencies](https://github.com/mrbean-bremen/pytest-find-dependencies),
pytest-envvars and pytest-isolate were surveyed as prior art; they are not used.
The `run.json`, `events.jsonl` and `report.json` formats (`schema_version` 1) are
defined by this project.

The names of these projects belong to their maintainers. qawrench is not affiliated
with or endorsed by any of them.

## msw-contract (formerly msw-contract-check)

The command, its bundled Node runner (`src/qawrench/msw_contract/node/runner.mjs`), the
example handler and cases in `examples/msw-contract/`, and the authored regression fixtures
in `tests/msw_contract/fixtures/` (handler modules, `contract-3.0.yaml`, `slow-schema.json`)
are original work under the qawrench MIT license. The standalone project was an
MIT-licensed Node.js package that composed the npm libraries listed below; in qawrench the OpenAPI loading,
routing, schema validation, reporting and CLI were rewritten in Python, and only the
execution of the user's MSW handlers still happens in Node.js.

**Executed, never bundled.**

| Component | License | Role |
| --- | --- | --- |
| [MSW](https://github.com/mswjs/msw) (`msw`; tested 3.0.5, MSW 2.15 handlers also run) | MIT | Still executed for real: the bundled runner imports the `msw` package **installed in the user's project** and calls its `getResponse` with the user's handlers. qawrench does not ship or vendor MSW. |
| [Node.js](https://nodejs.org/) 24 or newer | MIT (with bundled third-party notices) | Runs `runner.mjs`, per-case worker processes and Node's permission model; found on the user's `PATH`, not shipped. |

**Python dependencies (extra `openapi`, installed separately).**

| Distribution | License | Use |
| --- | --- | --- |
| [PyYAML](https://github.com/yaml/pyyaml) (`>=6.0.1`) | MIT | YAML/JSON parsing of OpenAPI documents and case files, restricted to the YAML 1.2 core schema |
| [jsonschema](https://github.com/python-jsonschema/jsonschema) (`>=4.18`) | MIT | Draft-04 and 2020-12 response-body validation |
| [referencing](https://github.com/python-jsonschema/referencing) (installed with jsonschema) | MIT | Schema registry and `$ref` resolution inside the prepared document |

jsonschema also brings attrs, jsonschema-specifications and rpds-py (all MIT).

**npm libraries whose behaviour is reimplemented in Python.** The standalone tool depended
on these packages at runtime; none of them is installed, bundled or executed by qawrench.
Their observable behaviour, as relied on by the original checks and tests, is re-expressed
in `qawrench.msw_contract`:

| Library (version used by the standalone tool) | License | Behaviour reimplemented |
| --- | --- | --- |
| [@redocly/openapi-core](https://github.com/Redocly/redocly-cli) 2.77.0 | MIT | Local multi-file `bundle` with a bounded local-only resolver (128 files / 32 MiB, 10 MiB per file), remote references refused, literal example data (`doNotResolveExamples`), external schemas hoisted into `components/schemas`, static `$anchor` references (`openapi.py`) |
| [@apidevtools/swagger-parser](https://github.com/APIDevTools/swagger-parser) 13.1.0 (with json-schema-ref-parser) | MIT | Dereferencing of OpenAPI objects with `circular: "ignore"` and the original excluded-path matcher that keeps schema references and examples intact; a reduced structural check replaces full OpenAPI meta-schema validation (`openapi.py`) |
| [openapi-backend](https://github.com/openapistack/openapi-backend) 5.23.0 (`OpenAPIRouter`) | MIT | Operation routing: apiRoot stripping, exact path before path templates, longest literal template first, lowercase method match (`openapi.py`) |
| [@openapi-contrib/openapi-schema-to-json-schema](https://github.com/openapi-contrib/openapi-schema-to-json-schema) 4.0.0 | MIT | OpenAPI 3.0 schema to JSON Schema draft-04: `nullable`, `removeWriteOnly`, required cleanup, strict type check, removal of OpenAPI-only keywords (`openapi.py`) |
| [Ajv](https://github.com/ajv-validator/ajv) 8.20.0 and [ajv-draft-04](https://github.com/ajv-validator/ajv-draft-04) 1.0.0 | MIT | Error shape (`instancePath`, `keyword`, `schemaPath`, params such as `missingProperty`/`additionalProperty`/`unevaluatedProperty`), English error messages, `allErrors` ordering of `anyOf`/`oneOf` branches, `$ref` siblings applied in every draft, `1.0` accepted as integer, unknown-format warnings with `strict: false` (`schema.py`) |
| [ajv-formats](https://github.com/ajv-validator/ajv-formats) 3.0.1 | MIT | "full" format definitions (date, time, date-time, iso-time, iso-date-time, duration, uri, uri-reference, uri-template, url, email, hostname, ipv4, ipv6, regex, uuid, json-pointer, json-pointer-uri-fragment, relative-json-pointer) plus the OpenAPI `int32`/`int64`/`byte`/`float`/`double`/`binary`/`password` registrations of the original tool. The regular expressions in `schema.py` are derived from ajv-formats (Copyright (c) 2020 Evgeny Poberezkin, MIT); its `url` pattern credits Diego Perini's URL regex gist (MIT) and its `uri` pattern credits the jmrware.com RFC 3986 URI regular expressions. |
| [json-schema-traverse](https://github.com/epoberezkin/json-schema-traverse) 1.0.0 | MIT | Pre-order subschema walk with the 2020-12 keyword extensions the original added (`openapi.py`) |
| [jsonpointer](https://github.com/janl/node-jsonpointer) 5.0.1 | MIT | RFC 6901 lookups for `writeOnly` detection through `#/` references (`openapi.py`) |
| [yaml](https://github.com/eemeli/yaml) 2.9.1 | ISC | YAML 1.2 core-schema scalars, unique map keys, stringified non-string keys and the `maxAliasCount: 100` resource limit (approximated) (`inputs.py`) |
| Node.js built-ins `util.MIMEType`, WHATWG `URL`, Fetch `Headers`, `TextDecoder` labels | MIT (Node.js); WHATWG standards CC BY 4.0 | Media-type parsing, HTTP(S) URL normalization, header normalization and charset-label comparison subsets (`contract.py`, `inputs.py`) |

**Test-only npm packages** (declared in `tests/msw_contract/fixtures/package.json`, installed
by developers with `npm install --prefix tests/msw_contract/fixtures --ignore-scripts`, never
shipped): `msw` (MIT), `msw2` (an alias of `msw@2`, MIT), [@msw/source](https://github.com/mswjs/source)
0.7.0 (MIT; generates handlers from the upstream Petstore document during a test),
[openapi-msw](https://github.com/christoph-fricke/openapi-msw) 2.1.1 (MIT) and `yaml` (ISC).
`examples/msw-contract/package.json` likewise only declares `msw` for running the example.

**Copied upstream material.** Two unmodified OpenAPI documents, downloaded 2026-11-11, are
carried as fixtures; their repository licenses are kept next to them and in
[`licenses/msw-contract/`](licenses/msw-contract) together with a `SOURCES.md`:

| File | Upstream | License |
| --- | --- | --- |
| `tests/msw_contract/fixtures/upstream/petstore-3.0.yaml`, `examples/msw-contract/petstore-3.0.yaml` | [OpenAPI Initiative OpenAPI-Specification](https://github.com/OAI/OpenAPI-Specification), archived passing 3.0 Petstore example (`_archive_/schemas/v3.0/pass/petstore.yaml`) | Apache-2.0 (`LICENSE-oai.txt`); the document's own `info.license` (MIT) describes the sample API |
| `tests/msw_contract/fixtures/upstream/petstore-3.1.yaml` | [OpenAPITools/openapi-generator](https://github.com/OpenAPITools/openapi-generator), `modules/openapi-generator/src/test/resources/3_1/java/petstore.yaml` | Apache-2.0 (`LICENSE-openapi-generator.txt`) |

SHA-256: `598136cb904e17e8eeead51ae33dd8d401fdff455d2d74f3869c4aa5f2742266` (3.0) and
`99fb0bf90c8ccd29551f09cf0a4e569f5f2325b141720539b8a6f34fb3a9fd08` (3.1).

**Prior art referenced, not used.** jest-openapi, @pactflow/pact-msw-adapter and Orval were
compared in the original survey (documentation inspected); none is installed, copied or
executed.

**Trademarks.** MSW (Mock Service Worker) is a project of its maintainers; OpenAPI is a
trademark of the Linux Foundation; Node.js is a trademark of the OpenJS Foundation; Redocly,
Swagger and SmartBear/PactFlow names belong to their owners. qawrench is not affiliated with
or endorsed by any of them; the names are used only to describe compatibility.

## mermaid-paths (formerly the standalone mermaid-paths)

The command code, the HTML/CSS it generates, the original example diagrams and review
files in `examples/mermaid-paths/` (`review-process.*`, `approval-zh.*`,
`workflow-doc.md`) and the tests in `tests/mermaid_paths/` are original work under the
qawrench MIT license, except the Mermaid fixtures noted below. The standalone project
was a Node.js package released under the MIT License (Copyright (c) 2026 tianhaogutum) and is ported to Python here under the same license.

**Runtime and test dependencies.** None beyond the Python standard library (Python is
covered by the PSF License Agreement, <https://docs.python.org/3/license.html>). The
tests use pytest. No Node.js runtime, npm package, browser or Mermaid installation is
needed, bundled or executed.

**Behaviour reimplemented from npm libraries.** The standalone tool was a Node.js
program built on the libraries below. qawrench contains independent Python
reimplementations of the behaviour the tool relied on; no JavaScript source is
vendored, linked or executed. The parts that follow published specifications or data
closely are noted.

| Library (version the original pinned) | License | What was reimplemented |
| --- | --- | --- |
| [@mmd-toolkit/parser](https://github.com/mmd-toolkit/mmd-toolkit/tree/main/packages/parser) 0.3.1 and [@mmd-toolkit/ast](https://github.com/mmd-toolkit/mmd-toolkit) 0.3.1 | MIT | Mermaid source rules (frontmatter, `%%` comment lines, `%%{…}%%` directives, entities, HTML attribute quotes, the `style`/`classDef` semicolon rule), diagram-type detection, and the flowchart lexer contexts and grammar, written from the package's documented rules (`src/qawrench/mermaid_paths/mermaid.py`). The table of Unicode code points read as flowchart "letters" (`_letters.py`) is transcribed from it; that table originates in Mermaid's flowchart grammar. Shape names, arrow marker names and error wording follow its AST and messages. |
| [@dagrejs/graphlib](https://github.com/dagrejs/graphlib) 4.0.7 | MIT | Strongly connected components (Tarjan) and unit-weight Dijkstra shortest paths, including graphlib's binary-heap priority queue and its tie-breaking, so loop return routes are identical (`diagram.py`). |
| [marked](https://github.com/markedjs/marked) 18.2.0 | MIT | GFM block tokenizer rules and algorithms (fences, indented code, headings, thematic breaks, block quotes, lists, HTML blocks, definitions, tables, paragraphs) used to find Mermaid code fences and render Markdown labels; inline rendering is a simplified equivalent (`_marked.py`). Several block-level regular expressions are adapted from Marked's sources. |
| [parse5](https://github.com/inikulin/parse5) 8.1.1 | MIT | Reading text out of label HTML: an HTML5-style tokenizer with WHATWG character-reference decoding (using the standard library's `html.entities` table) and the tool's own label walk (`htmltext.py`). Only the effects of tree construction that change readable text are approximated. |
| [html-validate](https://gitlab.com/html-validate/html-validate) 11.21.0 (dev dependency) | MIT | Not reimplemented. The original HTML conformance tests are replaced by a smaller standard-library structural checker in `tests/mermaid_paths/support.py`. |

Language tags are validated and canonicalized in Python following the Unicode BCP 47
locale-identifier syntax (UTS #35), with a small built-in subset of CLDR alias data
instead of the JavaScript runtime's `Intl.Locale`.

**Copied upstream material.** `tests/mermaid_paths/fixtures/upstream/flowchart-*.mmd`
are 110 examples extracted unchanged from the Mermaid 11.12.0 flowchart documentation
([`packages/mermaid/src/docs/syntax/flowchart.md`](https://github.com/mermaid-js/mermaid/blob/mermaid%4011.12.0/packages/mermaid/src/docs/syntax/flowchart.md)),
Copyright (c) 2014 - 2022 Knut Sveidqvist, MIT License. The license text is kept next to
the fixtures (`tests/mermaid_paths/fixtures/upstream/LICENSE.mermaid`) and in
[`licenses/mermaid-paths/LICENSE.mermaid`](licenses/mermaid-paths/LICENSE.mermaid). The
accompanying `manifest.json` (provenance, digests, scope decisions) and
`expected.json` (node labels and edges recorded once from the official Mermaid 11.12.0
parser, run under jsdom 26.1.0 (MIT) without rendering) are factual test data
produced by the original project. Mermaid and jsdom are not distributed.

**Formats and specifications referenced:** Mermaid flowchart syntax (11.12.0, with
the supported fixtures also checked against 12.3.0); CommonMark/GitHub Flavored
Markdown as implemented by Marked; the WHATWG HTML tokenizer and named character
references; BCP 47 / UTS #35 language tags; the WAI-ARIA Authoring Practices link and
landmark guidance. The `mermaid-paths/graph-v1` and `mermaid-paths/review-v1` JSON
formats are defined by this project and kept unchanged.

**Trademarks.** Mermaid is a project of its maintainers. qawrench is not affiliated with
or endorsed by the Mermaid, mmd-toolkit, Marked, parse5, graphlib, html-validate or
mermaid-a11y projects; the names only identify the syntax and behaviour reimplemented.
