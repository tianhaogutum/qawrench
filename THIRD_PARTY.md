# Third-party sources and attribution

qawrench is licensed under the MIT License (see [LICENSE](LICENSE)). Its commands were
previously separate MIT-licensed projects written by the same author and are merged
here under the same license. Unless a section below says otherwise, all code,
synthetic fixtures, tests and documentation are original work; they contain no
production captures, customer data, credentials or real-site recordings.

qawrench is an independent interoperability project. It is not affiliated with or
endorsed by Microsoft (Playwright), Google (Lighthouse, Chrome), Deque Systems (axe),
the Pa11y project, Apify (Crawlee), the Apache Software Foundation (Arrow, Parquet),
the lycheeverse maintainers (Lychee), OASIS (SARIF), Scrapinghub/Zyte (extruct, w3lib)
or any other project named below. Product names only identify interoperable file
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
| [pytest](https://github.com/pytest-dev/pytest) | MIT | test runner | test |
| [jsonschema](https://github.com/python-jsonschema/jsonschema) | MIT | `a11y` SARIF schema tests | test |
| [rfc3986-validator](https://github.com/naimetti/rfc3986-validator) | MIT | `uri` format checks in `a11y` tests | test |
| [Hatchling](https://github.com/pypa/hatch) | MIT | build backend | build |
| [hatch-fancy-pypi-readme](https://github.com/hynek/hatch-fancy-pypi-readme) | MIT | PyPI README link rewriting | build |

Python and its standard library (including `sqlite3`, `zipfile` and `zlib`) are
provided by the user's Python installation under the
[PSF License Agreement and bundled notices](https://docs.python.org/3/license.html).
SQLite is [public domain](https://www.sqlite.org/copyright.html). No SQLite or zlib
source is vendored.

### License texts in `licenses/`

[`licenses/`](licenses) keeps the license and notice files the standalone projects
recorded for the dependency versions they were tested with (copied unchanged from the
installed distributions). They document those dependencies; the files are not shipped
inside the wheel, and none of these packages is vendored.

| Directory | Contents |
| --- | --- |
| [`licenses/crawlee-export/`](licenses/crawlee-export) | PyArrow LICENSE and NOTICE, Crawlee LICENSE |
| [`licenses/frontier/`](licenses/frontier) | w3lib 2.3.1 |
| [`licenses/page-diff/`](licenses/page-diff) | beautifulsoup4 4.14.3, soupsieve 2.8.1, typing_extensions 4.15.0 |
| [`licenses/structured-data/`](licenses/structured-data) | extruct 0.18.0, lxml 6.1.3 and the rest of extruct's tested dependency closure |

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

