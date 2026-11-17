# qawrench msw-contract: upstream OpenAPI documents

`qawrench msw-contract` (formerly `msw-contract-check`) carries two unmodified upstream
OpenAPI documents as test fixtures and one of them as an example:

| File | Upstream | Repository license |
| --- | --- | --- |
| `tests/msw_contract/fixtures/upstream/petstore-3.0.yaml`, `examples/msw-contract/petstore-3.0.yaml` | OpenAPI Initiative, archived passing OpenAPI 3.0 Petstore example, downloaded 2026-11-11 from <https://raw.githubusercontent.com/OAI/OpenAPI-Specification/main/_archive_/schemas/v3.0/pass/petstore.yaml> | Apache-2.0 ([`LICENSE-oai.txt`](LICENSE-oai.txt)) |
| `tests/msw_contract/fixtures/upstream/petstore-3.1.yaml` | OpenAPI Generator OpenAPI 3.1 Java fixture, downloaded 2026-11-11 from <https://raw.githubusercontent.com/OpenAPITools/openapi-generator/master/modules/openapi-generator/src/test/resources/3_1/java/petstore.yaml> (project: <https://github.com/OpenAPITools/openapi-generator>) | Apache-2.0 ([`LICENSE-openapi-generator.txt`](LICENSE-openapi-generator.txt)) |

The Petstore documents' own `info.license` fields (MIT for the OAI example, Apache-2.0 for
the OpenAPI Generator fixture) describe the sample APIs and are recorded separately from
repository licensing. The license texts are also kept next to each copied document.

SHA-256 of the copied documents:

```text
598136cb904e17e8eeead51ae33dd8d401fdff455d2d74f3869c4aa5f2742266  petstore-3.0.yaml
99fb0bf90c8ccd29551f09cf0a4e569f5f2325b141720539b8a6f34fb3a9fd08  petstore-3.1.yaml
```
