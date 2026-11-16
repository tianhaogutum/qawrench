# Tested upstream surface

Reviewed on 2026-11-11 for `qawrench frontier` (formerly `crawl-frontier-audit`).
Back to [the command documentation](../frontier.md).

## w3lib 2.3.1

Primary source: [tagged url.py](https://github.com/scrapy/w3lib/blob/v2.3.1/w3lib/url.py).
The installed function was also inspected with `inspect.getsource`.
The [current reference](https://w3lib.readthedocs.io/en/latest/w3lib.html#w3lib.url.canonicalize_url)
is newer (2.6.0), so it is **not** a compatibility promise for the tested version 2.3.1.
Our integration passes a fixed dummy HTTPS URL containing only the caller's
query to `canonicalize_url`, explicitly retaining blank values and fragments,
and extracts the resulting query. Host/path transformations therefore cannot
leak into the source URL. This intentionally differs from full URL
canonicalization. Tests exercise real percent-encoded bytes and repeated pairs.

Sorting orders keys and values and reserializes query data: spelling, bare
flags, empty separators, and space encodings can change. Repeated nonempty
pairs retain multiplicity. This is opt-in and can be unsafe for an application.
IDNA conversion is separately opt-in using Python's standard IDNA codec.

## Crawlee JavaScript 3.18 documented JSON sources subset

Primary references: [RequestList](https://crawlee.dev/js/api/core/class/RequestList)
and [RequestOptions](https://crawlee.dev/js/api/core/interface/RequestOptions),
both displaying version 3.18 at review time.
The `RequestList.open` sources argument supports strings and request objects;
the offline adapter accepts a local JSON array with those forms.
It maps string `payload` to exact `body`, defaults method to GET, and preserves
`userData`/`label` as metadata. There is no claim of implementing RequestList
persistence, its state files, or its full option set.

Crawlee supports custom `uniqueKey`, method/payload-aware extended keys,
fragment controls and remote lists. This simulator uses its own policy keys;
it rejects those options rather than silently bypassing their semantics.
Header-bearing requests, runtime objects and binary payloads are unsupported.
No Node package is needed. The fixture is synthetic, authored from the public
shape; it was not exported from a running Crawlee instance. Tests exercise the
adapter and round-trip its retained metadata through our JSONL output.
