#!/usr/bin/env python3
"""Sequential offline comparison of SYNTHETIC ZIPs, including no-gain cases."""

import argparse
import hashlib
import json
import platform
from pathlib import Path
import sys
import tempfile
import time
import zipfile
import zlib

# Allow `python3 examples/trace_budget/benchmark.py` from a qawrench checkout (src layout).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from qawrench.trace_budget import repack_archive  # noqa: E402
from generate_trace import generate  # noqa: E402


def hashes(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("CRC validation failed")
        return {info.filename: hashlib.sha256(archive.read(info)).hexdigest() for info in archive.infolist()}


def benchmark() -> dict:
    results = []
    with tempfile.TemporaryDirectory(prefix="trace-budget-benchmark-") as directory:
        root = Path(directory)
        for kind in ("stored-text-heavy", "precompressed-binary", "incompressible-binary"):
            source, dest = root / f"{kind}.zip", root / f"{kind}-repacked.zip"
            start = time.perf_counter()
            generate(source, kind)
            generate_seconds = time.perf_counter() - start
            start = time.perf_counter()
            before = hashes(source)
            hash_seconds = time.perf_counter() - start
            result = repack_archive(source, dest)
            after = hashes(dest)
            if before != after:
                raise RuntimeError("independent benchmark member-hash comparison failed")
            result.update({"fixture": kind, "synthetic": True, "generate_seconds": generate_seconds,
                           "baseline_hash_seconds": hash_seconds, "member_hashes_before": before,
                           "member_hashes_after": after, "independent_hash_check": True})
            results.append(result)
    return {"synthetic_only": True, "python": platform.python_version(), "platform": platform.platform(),
            "zlib_runtime": zlib.ZLIB_RUNTIME_VERSION,
            "method": "one sequential run per fixture; elapsed repack includes input validation, compression, reopened CRC/SHA verification and atomic publish",
            "results": results,
            "limitations": "Synthetic offline workloads only; timings vary; no evidence of real-site savings or viewer compatibility."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="write a new benchmark JSON file (exclusive create)")
    args = parser.parse_args()
    report = benchmark()
    output = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out:
        with args.out.open("x", encoding="utf-8") as handle:
            handle.write(output)
    print(output, end="")


if __name__ == "__main__":
    main()
