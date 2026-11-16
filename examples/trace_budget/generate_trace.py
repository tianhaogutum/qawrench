#!/usr/bin/env python3
"""Generate SYNTHETIC trace-shaped ZIPs. No browser or production data involved."""

import argparse
import hashlib
import json
from pathlib import Path
import random
import stat
import zipfile
import zlib


def ndjson(events: list[dict]) -> bytes:
    return b"".join(json.dumps(e, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n" for e in events)


def members(kind: str = "stored-text-heavy") -> dict[str, bytes]:
    """Original synthetic data using the documented v1.56.1/version-8 envelopes."""
    if kind not in ("stored-text-heavy", "precompressed-binary", "incompressible-binary"):
        raise ValueError(f"unknown fixture kind: {kind}")
    context = {"version": 8, "type": "context-options", "origin": "library", "browserName": "chromium",
               "platform": "linux", "wallTime": 1700000000000, "monotonicTime": 100,
               "sdkLanguage": "python", "options": {"viewport": {"width": 800, "height": 600}},
               "title": "SYNTHETIC fixture, not a browser recording"}
    events = [context]
    if kind == "stored-text-heavy":
        payload = (b"<p>SYNTHETIC repeated offline response, no real site.</p>\n" * 6000)
        for i in range(1200):
            events.extend([
                {"type": "before", "callId": f"call@{i}", "startTime": 100 + i * 2,
                 "class": "Page", "method": "goto", "params": {"url": "https://fixture.invalid/example"}},
                {"type": "after", "callId": f"call@{i}", "endTime": 101 + i * 2},
            ])
    else:
        random_bytes = random.Random(36).randbytes(2 * 1024 * 1024)
        payload = zlib.compress(random_bytes, 9) if kind == "precompressed-binary" else random_bytes
    resource = hashlib.sha1(payload).hexdigest()
    network = {"type": "resource-snapshot", "snapshot": {
        "pageref": "page@synthetic", "startedDateTime": "2023-11-14T22:13:20.000Z", "time": 1,
        "request": {"method": "GET", "url": "https://fixture.invalid/example", "httpVersion": "HTTP/1.1",
                    "headers": [], "queryString": [], "cookies": [], "headersSize": -1, "bodySize": 0},
        "response": {"status": 200, "statusText": "OK", "httpVersion": "HTTP/1.1", "headers": [],
                     "cookies": [], "content": {"size": len(payload), "mimeType": "application/octet-stream", "_sha1": resource},
                     "redirectURL": "", "headersSize": -1, "bodySize": len(payload)},
        "cache": {}, "timings": {"send": 0, "wait": 1, "receive": 0}, "_frameref": "frame@synthetic"}}
    return {"trace.trace": ndjson(events), "trace.network": ndjson([network]),
            f"resources/{resource}": payload,
            "fixture-label.txt": b"SYNTHETIC offline fixture. Not produced by Playwright.\n"}


def generate(path: str | Path, kind: str = "stored-text-heavy") -> Path:
    target = Path(path)
    compression = zipfile.ZIP_DEFLATED if kind == "precompressed-binary" else zipfile.ZIP_STORED
    with zipfile.ZipFile(target, "x", compression=compression, compresslevel=9) as archive:
        for name, data in sorted(members(kind).items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = compression
            info._compresslevel = 9
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, data)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output")
    parser.add_argument("--kind", choices=("stored-text-heavy", "precompressed-binary", "incompressible-binary"), default="stored-text-heavy")
    args = parser.parse_args()
    path = generate(args.output, args.kind)
    print(f"SYNTHETIC {args.kind}: {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
