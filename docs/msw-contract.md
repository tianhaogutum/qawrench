# qawrench msw-contract

`qawrench msw-contract` (formerly `msw-contract-check`) catches **stale MSW mocks**: it
executes your existing [MSW](https://mswjs.io/) HTTP handlers against explicit request
examples and checks the **actual returned status, Content-Type and response body** against
your OpenAPI 3.0/3.1 contract. Use it in CI next to the front-end tests that rely on those
mocks: when the API contract moves on but a handler still returns the old shape, your
application tests keep passing while this check fails with a field-level report.

It does not generate replacement mocks, start a server or call a real backend. Only the
request branches you list are exercised.

## How it runs

MSW handlers are JavaScript, so they are executed for real by a **small Node.js runner
bundled with qawrench** (`qawrench/msw_contract/node/runner.mjs`). The runner resolves the
**`msw` package installed in your own project** (from the handlers file's directory upwards,
then from the working directory), forks one fresh, bounded Node process per request case,
calls MSW's `getResponse` with a Fetch `Request`, and hands the actual status, headers and
decoded body back to Python.

Everything else is Python: reading cases, loading and bundling the OpenAPI document,
routing requests to operations, selecting the documented response, validating the body
against the schema, reporting, exit codes and the CLI.

Requirements, checked before any case runs:

- **Node.js 24 or newer** on `PATH` (found with `shutil.which("node")`).
- **`msw` installed in the project that owns the handlers** (`npm install --save-dev msw`).
  MSW 3 is the tested version; MSW 2 HTTP handlers also run.

If Node.js is missing or too old, or `msw` cannot be resolved, the command prints one line
such as `qawrench msw-contract: Node.js was not found on PATH; ...` and exits **2**.

## Install

Requires Python 3.11 or newer. The OpenAPI checks need PyYAML and jsonschema, installed by
the `openapi` extra:

```sh
pipx install 'qawrench[openapi]'      # or: pip install 'qawrench[openapi]'
qawrench msw-contract --version       # prints the bare qawrench version, e.g. 0.1.0
```

Without the extra, the command prints an install hint
(`qawrench msw-contract needs yaml: pipx install 'qawrench[openapi]' ...`) and exits 2.
`python -m qawrench msw-contract` is equivalent to `qawrench msw-contract`.

## Quick start

The example in a qawrench source checkout checks a deliberately stale handler against the
real, unmodified OAI Petstore contract. Install `msw` for the example once, then run it from
the repository root:

```sh
npm install --prefix examples/msw-contract --ignore-scripts
qawrench msw-contract \
  --handlers examples/msw-contract/handlers.mjs \
  --schema examples/msw-contract/petstore-3.0.yaml \
  --cases examples/msw-contract/cases.json \
  --api-root /v1
```

It exits **1**, catching a string `id` where the contract requires an integer:

```text
2/3 examples passed; 1 failed (explicit examples only)
PASS list-pets: GET http://petstore.swagger.io/v1/pets?limit=2 -> 200 application/json
FAIL stale-id: GET http://petstore.swagger.io/v1/pets/1 -> 200 application/json
  schema /id: must be integer; actual "1"
  response sample: "{\"id\":\"1\",\"name\":\"Milo\"}"
PASS missing-pet: GET http://petstore.swagger.io/v1/pets/missing -> 404 application/json
```

Change `id: params.petId` to `id: Number(params.petId)` in
`examples/msw-contract/handlers.mjs` and the same three cases pass. The contract is upstream
YAML, not a schema invented to fit the handler (see `examples/msw-contract/SOURCES.md`).

## Use your handlers

Run from your application's root, so the handlers' imports and your `node_modules` are
readable:

```sh
qawrench msw-contract \
  --handlers ./src/mocks/handlers.mjs \
  --schema ./openapi.yaml \
  --cases ./contract-cases.json \
  --base-url https://api.example.test/ \
  --api-root /v1 \
  --format json --output ./contract-report.json
```

| Option | Meaning |
| --- | --- |
| `--handlers FILE` | Handler module (required). ESM `.mjs`, CommonJS `.cjs`, or Node-native erasable TypeScript `.ts`. |
| `--schema FILE` | OpenAPI 3.0.x or 3.1.x document, YAML or JSON (required). |
| `--cases FILE` | JSON or YAML array of explicit request cases (required). |
| `--base-url URL` | Resolve relative case URLs and relative MSW handler patterns against this URL. |
| `--api-root PATH` | Strip this URL path prefix before matching OpenAPI paths (default `/`). |
| `--export NAME` | Named handler-array export (default `handlers`; a default array or a default object with `handlers` is also accepted). |
| `--timeout MS` | Per-case deadline covering import, execution, body capture and validation (default 3000; max 120000). |
| `--max-body-bytes N` | Reject larger responses (default 1048576; max 10485760). |
| `--sample-bytes N` | Capture up to N response bytes in reports (default 2048; 0 disables samples; max 65536). |
| `--format text\|json` | Output format (default `text`). |
| `--output FILE` | Write the report to FILE instead of stdout. |
| `--version` | Print the qawrench version. |

The module must export real MSW HTTP handlers; `getResponse` performs MSW's own request
matching, resolver execution and handler precedence. Browser globals, bundler aliases and
transpiler-specific syntax are out of scope.

## Cases

Cases are a UTF-8 JSON or YAML array (at most 1000 entries, 4 MiB). Each case names one
explicit, reachable branch:

```json
[
  { "id": "existing-user", "method": "GET", "url": "/v1/users/42" },
  { "id": "missing-user", "url": "/v1/users/missing" },
  {
    "id": "create-user",
    "method": "POST",
    "url": "/v1/users",
    "headers": { "x-scenario": "created" },
    "json": { "name": "Milo" }
  }
]
```

- `id` is optional and must be unique when supplied (default `case-N`). `method` defaults to
  `GET`. Unknown fields are rejected.
- Use absolute HTTP(S) URLs or supply `--base-url`. The same explicit base is passed to MSW
  to resolve relative handler patterns; no request origin is guessed. Normal URL resolution
  applies: a leading `/` replaces the base path; a relative `users` keeps a trailing base
  path such as `/v1/`.
- `json` sets an `application/json` Content-Type if absent; `body` supplies a raw string
  instead. Choose one. GET and HEAD cannot carry a body.
- `--api-root` strips an explicit path prefix before matching OpenAPI paths. Use `/v1` when
  requests contain `/v1/pets` but the contract path is `/pets`. OpenAPI server origins are
  not enforced; routing compares the request method and path.
- Each case gets a fresh Node process and module instance. State mutations, generators and
  MSW `{ once: true }` handlers do not persist between cases: this checks independent
  branches, not multi-request workflows.

## What is checked

OpenAPI 3.0.x and 3.1.x documents (YAML 1.2 or JSON) are accepted; 3.1 documents may use
the default OAS dialect or JSON Schema 2020-12. Local multi-file references are bundled into
the document first; OpenAPI objects (responses, parameters, path items) are then
dereferenced while schema references stay intact for the validator, so 3.1 `$ref` sibling
constraints are not overwritten. Remote references are refused, and literal `$ref` fields in
examples remain data.

**Status.** Response selection follows the exact status, then its `2XX`/`4XX` range, then
`default`. An undocumented status fails.

**Media type.** Matching prefers an exact type over `type/*` over `*/*`, then the entry with
the most matching declared parameters. Type names and charset values compare
case-insensitively; other parameter values compare literally. Extra response parameters are
allowed. Equally specific matches fail as ambiguous. This follows the
[OpenAPI Response Object](https://spec.openapis.org/oas/v3.1.1.html#response-object) and
[HTTP media-type semantics](https://www.rfc-editor.org/rfc/rfc9110.html#name-media-type).

**Body.** JSON and `+json` media are parsed as JSON; `text/*` media are checked as strings.
OpenAPI 3.0 schemas are converted to JSON Schema draft-04 (`nullable`, boolean exclusive
bounds, response-only `writeOnly` handling) and validated with a draft-04 validator; 3.1
schemas use JSON Schema 2020-12 (tuples, `unevaluatedProperties`, boolean schemas, static
`$anchor`). Required `writeOnly` properties, including those reached through `$ref` or
`allOf`, need not appear in responses; `readOnly` ones must. Standard formats (date,
date-time, email, uri, uuid, ipv4/ipv6, ...) and the OpenAPI `int32`, `int64` and `byte`
formats are checked; unknown formats are reported as warnings and not enforced. Data is
never coerced, defaulted or stripped to make it pass.

Errors carry JSON Pointer field paths, the failing schema keyword, the schema path and a
bounded actual value; the response sample is bounded separately. A media entry without a
schema still gets status, media and JSON-syntax checks and reports
`bodyValidation: "no-schema"`. A response that declares no content must be empty. HEAD
requests skip the body after status and media selection. At most 20 schema errors are
listed per case, with the full count.

## Process limits and failures

Cases run sequentially. Each handler runs in its own Node process with a 256 MB heap, a
minimal environment (`PATH`, `TZ=UTC`, `LANG=C.UTF-8`, `NODE_ENV=test`) and Node's permission
model: it can read the working directory, the runner's directory and the `node_modules`
folder that contains `msw`, but cannot write files, start subprocesses, load native addons or
create worker threads. Common outbound fetch, HTTP, socket and DNS entry points are blocked,
and MSW passthrough is reported without making a real request. Response capture is streamed
and capped; handler output is captured as diagnostics and capped at 8 KiB.

The default case deadline is 3000 ms, covering module import, resolver execution, reading
the body and response validation. A timeout kills the case's process and later cases
continue. Schema validation runs in a separate Python process that caches compiled
validators; if it stalls (for example on a pathological `pattern`) it is killed, the case
reports `validation-timeout`, and a fresh validator is started for the next case. Loading
the OpenAPI document has its own 10-second deadline. SIGINT and SIGTERM stop all child
processes (exit 130 and 143).

**Use trusted local handler modules.** Process isolation and the network guards provide
reproducible failure containment, not a security boundary for hostile JavaScript.

Failure codes reported per case include `handler-error`, `timeout`, `log-limit`,
`body-limit`, `body-stream`, `body-encoding`, `passthrough`, `network-error`,
`unhandled-request`, `invalid-status`, `operation-not-found`, `status`, `media-type`,
`media-type-ambiguous`, `unexpected-body`, `invalid-json`, `unsupported-media`, `schema`,
`schema-error`, `validation-timeout` and `validation-process`.

## Reports and exit codes

Exit **0** means every explicit example passed, **1** means at least one example failed
(including handler execution failures), and **2** means invalid input or configuration,
including a missing Node.js, an unresolvable `msw` or a missing `openapi` extra. Errors are
printed as one line, `qawrench msw-contract: <message>`.

JSON output (`--format json`) is unchanged from the standalone tool: an object with
`version` (1), `scope` (`"explicit-examples-only"`), `summary` (`total`, `passed`,
`failed`) and `results` in case order. Each result has `id`, `request`, `ok` and `issues`,
plus `operation`, `statusKey`, `mediaType`, `bodyValidation`, `issueCount`,
`issuesTruncated`, `warnings`, `response` (`status`, `contentType`, `bytes`, `sample`,
`sampleTruncated`), `diagnostics` (handler output) and `validationDiagnostics` when they
apply. Handler output never mixes into the JSON stream. `--output` files are replaced
atomically with mode 0600, so a failed write keeps the previous report.

## Limits

- Only the provided requests are exercised. Passing examples do not prove that all handlers,
  statuses or branches, request validity or the live backend are correct.
- Response headers other than Content-Type are not schema-validated.
- MSW v1 `rest`/`res`, GraphQL, WebSockets and service workers are out of scope.
- XML, multipart and binary schema bodies fail as unsupported instead of being silently
  accepted; media without a schema still get status and media checks.
- Custom dialects, schema `$id`, `$dynamicRef` and `$dynamicAnchor` are rejected; static
  `$anchor` references work. `pattern` and the `regex` format use Python regular
  expressions, which differ from ECMAScript in some escapes and Unicode classes.
  Discriminators and OpenAPI extensions add no validation beyond the schema constraints.
  `writeOnly` handling covers direct properties and unconditional `$ref`/`allOf` chains, not
  branch-dependent compositions.
- Structural validation of the OpenAPI document is limited to what the checks rely on
  (info, paths, operations, response keys and descriptions); it is not a full OpenAPI
  meta-schema validation.
- Input files are capped at 10 MiB (cases 4 MiB) and 1000 cases; local references at 128
  files, 10 MiB each and 32 MiB in total. Referenced files must be readable from the schema
  file's location.

## Library use

```python
from qawrench.msw_contract import check, format_text

report = check(
    handlers_path="./src/mocks/handlers.ts",
    schema_path="./openapi.yaml",
    cases=[{"id": "existing-user", "url": "/users/42"}],
    base_url="https://api.example.test/",
    sample_bytes=0,
)
if report["summary"]["failed"]:
    raise AssertionError(format_text(report))
```

Supply exactly one of `cases` or `cases_path`. `cwd` defaults to the current directory;
`api_root`, `export_name`, `timeout_ms`, `max_body_bytes` and `sample_bytes` match the CLI
options. An optional `cancel` (`threading.Event`) stops and reaps the child processes and
raises `CheckCancelled`; `node` selects a specific Node.js executable. Invalid input raises
`CheckError`; a missing Node.js or `msw` raises its subclass `RuntimeUnavailable`.
`load_contract` and `check_response` run the OpenAPI checks on their own, and `run_handler`
executes a single normalized case.

## Tests

From a qawrench checkout:

```sh
uv run --extra test pytest tests/msw_contract -q
```

Tests that execute real handlers need Node.js 24+ and the npm packages listed in
`tests/msw_contract/fixtures/package.json`:

```sh
npm install --prefix tests/msw_contract/fixtures --ignore-scripts
```

Without them those tests are skipped; the OpenAPI loading, routing, media selection,
schema, input and output tests use synthetic runner output and need neither Node nor `msw`.
Fixtures include the unmodified OAI Petstore 3.0 document and OpenAPI Generator's 3.1
Petstore fixture (provenance and licenses in `tests/msw_contract/fixtures/upstream/`), handlers
generated by `@msw/source`, `openapi-msw` helper handlers, MSW 2 and 3 modules, and authored
regression fixtures for branches, media selection, timeouts, references and schema edge
cases.

## Prior art

The gap this command fills is running existing, possibly stale handlers against explicit
requests and checking their actual responses locally. Closest tools, as surveyed for the
standalone release:

- **MSW** `getResponse` executes real resolvers without a server but performs no OpenAPI
  checking; it is the execution engine here.
- **OpenAPI Backend** routes and validates requests, but its response validation only
  selects `application/json` schemas and accepts undocumented statuses when no default
  exists; its routing rules are reproduced here.
- **OpenAPI-MSW** adds compile-time typing to handlers; runtime JavaScript and untyped
  fixtures can still drift. Its handlers are ordinary MSW handlers and are audited unchanged.
- **@msw/source** and **Orval** generate mocks from OpenAPI; generation replaces the data
  source rather than auditing existing custom resolver branches.
- **jest-openapi** asserts a response supplied by a test against OpenAPI, but does not
  import, isolate or drive MSW handlers.
- **@pactflow/pact-msw-adapter** records MSW interactions into Pact contracts for
  bi-directional contract testing, not a local OpenAPI response audit.
- **Swagger Parser**, **Redocly OpenAPI Core**, **Ajv**/**ajv-formats**, **OpenAPI Schema to
  JSON Schema** and **jsonpointer** supplied parsing, bundling, conversion and validation in
  the standalone tool; their behaviour is reimplemented in Python on PyYAML and jsonschema.

## License

MIT; see [LICENSE](../LICENSE). See [THIRD_PARTY.md](../THIRD_PARTY.md) for the upstream
OpenAPI fixtures, their Apache-2.0 licenses (also in
[`licenses/msw-contract/`](../licenses/msw-contract)) and the libraries whose behaviour is
reproduced.
