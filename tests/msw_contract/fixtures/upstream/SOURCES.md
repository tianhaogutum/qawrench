# Fixture provenance

- `petstore-3.0.yaml` is an unmodified download (2026-11-11) of the OpenAPI Initiative's archived passing OpenAPI 3.0 Petstore example: <https://raw.githubusercontent.com/OAI/OpenAPI-Specification/main/_archive_/schemas/v3.0/pass/petstore.yaml>. The document identifies its license as MIT. It has real response `$ref`s, path parameters, int64/int32 formats, a server base path, a bodyless 201, and default error responses.
- `petstore-3.1.yaml` is an unmodified download (2026-11-11) of OpenAPI Generator's real OpenAPI 3.1 Java fixture: <https://raw.githubusercontent.com/OpenAPITools/openapi-generator/master/modules/openapi-generator/src/test/resources/3_1/java/petstore.yaml>. The fixture identifies Apache-2.0 licensing; upstream project: <https://github.com/OpenAPITools/openapi-generator>. It includes 33 operations, mixed XML/JSON response media, shared response/request-body components, union types and tuple schemas.
- The handler modules in `..` use the real MSW API. `../generated.mjs` uses `@msw/source` to generate handlers from `petstore-3.0.yaml` during the Node-backed tests; the generated data is validated after actually calling the handlers.
- Every other spec and handler in `..` is an authored regression fixture, not upstream output. Their purpose is to expose branch, media selection, timeout, reference and schema edge cases.

The upstream repositories distribute their files under Apache-2.0; their license texts are kept next to the documents as `LICENSE-oai.txt` and `LICENSE-openapi-generator.txt` (copies are also in `licenses/msw-contract/` at the repository root). The Petstore documents' `info.license` fields describe the sample APIs and are recorded above separately from repository licensing.

SHA-256 of the exact committed fixtures:

```text
598136cb904e17e8eeead51ae33dd8d401fdff455d2d74f3869c4aa5f2742266  petstore-3.0.yaml
99fb0bf90c8ccd29551f09cf0a4e569f5f2325b141720539b8a6f34fb3a9fd08  petstore-3.1.yaml
```
