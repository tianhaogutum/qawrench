# Example sources

`petstore-3.0.yaml` is an unmodified copy of the OpenAPI Initiative's archived passing
OpenAPI 3.0 Petstore example
(<https://raw.githubusercontent.com/OAI/OpenAPI-Specification/main/_archive_/schemas/v3.0/pass/petstore.yaml>,
downloaded 2026-11-11, SHA-256
`598136cb904e17e8eeead51ae33dd8d401fdff455d2d74f3869c4aa5f2742266`). The OpenAPI
Specification repository is distributed under Apache-2.0; its license text is
`LICENSE-oai.txt`. The document's own `info.license` (MIT) describes the sample API.

`handlers.mjs` and `cases.json` are original example files. The handler is deliberately
stale: it returns the string path parameter as `id`, where the contract requires an integer.
