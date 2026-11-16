# Synthetic offline fixtures

All data in this directory was authored for this tool. It is not scraped,
production, or exported by a live Crawlee installation.

`dataset/` follows the local JavaScript Crawlee v3.15.3 source-inspected layout:
zero-padded numbered JSON object files and the known `__metadata__.json` filename.
The numeric gap deliberately tests sparse ordering; metadata is ignored, never
used to synthesize or omit rows. `items.jsonl` contains the same three objects in
physical line order. `schema.json` explicitly declares the adapter's version-1
schema, including the `json-string` opt-in for nested tags.

`verify_roundtrip.py` is independent of the exporter implementation and compares
actual PyArrow-read values to literal expectations for `dataset/`. Run without
Python's `-O` option, which disables assertions. The general CLI `--verify` uses
explicit checks, not assertions.

`evolution/` contains five source-shaped dataset items and equivalent JSONL with
an explicitly declared version-2 nullable union schema. `note` is absent, explicit
null, a Unicode/newline string, absent again, and empty string; nullable int64
`score` is never present. `verify_evolution.py` independently checks both formats'
literal values, field presence, types, policy metadata, summaries and digests.
Its mode argument is `dataset` or `jsonl`. Run without Python's `-O` option.
`evolution/schema-before.json` declares only the original required `id`; comparing
it to `evolution/schema.json` with `--check-schema` permits the nullable additions
`note` and `score` before a fresh export.
