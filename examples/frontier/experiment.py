#!/usr/bin/env python3
"""Seeded SYNTHETIC comparison, not evidence about live-site equivalence/speed."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict
import json
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time

from qawrench.frontier import Policy, load_requests, proposed_url

ROOT = Path(__file__).resolve().parents[2]


def seeded_records(seed: int = 39, groups: int = 100) -> list[dict]:
    records = []
    for i in range(groups):
        origin = f"https://example.test/{i}"
        def add(suffix: str, label: str, method: str = "GET", body: str = "") -> None:
            records.append({"url": origin + suffix, "method": method, "body": body,
                            "source": f"SYNTHETIC:{i}:{label}"})
        for suffix in ("/doc?v=1", "/doc?v=1&utm_source=a#top", "/doc?v=1&utm_source=b#end", "/doc?v=1", "/doc?v=1&utm_source=c"):
            add(suffix, "doc")
        add("/ordered?k=a&k=b&empty=", "ordered-ab")
        add("/ordered?k=b&k=a&empty=", "ordered-ba")
        add("/repeat?k=a&k=a", "repeat-aa")
        add("/repeat?k=a", "repeat-a")
        add("/blank?flag", "bare-flag")
        add("/blank?flag=", "assigned-empty-flag")
        add("/encoded?q=%2f", "slash")
        add("/encoded?q=%2F", "slash")
        add("/octets?q=%FF", "octet-ff")
        add("/octets?q=%FE", "octet-fe")
        for method, body in (("GET", ""), ("POST", "x=1"), ("POST", "x=2"), ("POST", "x=1 ")):
            add("/submit", f"submit:{method}:{body}", method, body)
        # This synthetic application's fragment is a meaningful client route.
        add("/spa#one", "spa-one")
        add("/spa#two", "spa-two")
        for host in ("bücher.test", "xn--bcher-kva.test"):
            records.append({"url": f"https://{host}/{i}", "source": f"SYNTHETIC:{i}:idna"})
    random.Random(seed).shuffle(records)
    return records


def label_stats(keys: list, labels: list[str]) -> dict:
    buckets = defaultdict(Counter)
    for key, label in zip(keys, labels):
        buckets[key][label] += 1
    mixed = [counter for counter in buckets.values() if len(counter) > 1]
    false_pairs = sum((sum(c.values()) ** 2 - sum(n * n for n in c.values())) // 2 for c in mixed)
    return {"unique": len(buckets), "false_merge_groups": len(mixed), "false_merge_pairs": false_pairs,
            "label_witnesses": [dict(sorted(c.items())) for c in mixed]}


def run(seed: int = 39, groups: int = 100, repeats: int = 5) -> dict:
    records = seeded_records(seed, groups)
    policy = Policy.from_dict(json.loads((ROOT / "examples/frontier/policy.json").read_text()))
    sorted_policy = Policy.from_dict(json.loads((ROOT / "examples/frontier/sort-policy.json").read_text()))
    tracking_only = Policy(remove_query_keys=("utm_source",), allow_ambiguous_merges=True)
    with tempfile.TemporaryDirectory(prefix="frontier-experiment-") as folder:
        root = Path(folder)
        input_path = root / "seeded.jsonl"
        input_path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        requests, issues, total = load_requests(input_path)
        assert not issues and total == len(records)
        # Exercise the real documented command and actual fixture output path.
        command = [sys.executable, "-m", "qawrench.frontier", str(input_path),
                   "--policy", str(ROOT / "examples/frontier/policy.json"),
                   "--frontier", str(root / "frontier.jsonl"), "--report", str(root / "report.json")]
        completed = subprocess.run(command, capture_output=True, text=True, check=True, cwd=ROOT)
        report = json.loads((root / "report.json").read_text())
        frontier = [json.loads(line) for line in (root / "frontier.jsonl").read_text().splitlines()]
        assert len(frontier) == report["counts"]["proposed_unique"]
        assert report["frontier_emitted"] and report["status"] == "ok"
    algorithms = {
        "raw_url_set": lambda r: r.url,
        "conservative": lambda r: r.exact_key(),
        "tracking_only": lambda r: (r.method, proposed_url(r.url, tracking_only), r.body),
        "proposed": lambda r: (r.method, proposed_url(r.url, policy), r.body),
        "proposed_with_w3lib_sort": lambda r: (r.method, proposed_url(r.url, sorted_policy), r.body),
    }
    labels = [r.original["source"] for r in requests]
    results = {}
    for name, key_fn in algorithms.items():
        keys = [key_fn(r) for r in requests]
        samples = []
        for _ in range(repeats):
            start = time.perf_counter_ns()
            unique = {key_fn(r) for r in requests}
            samples.append((time.perf_counter_ns() - start) / 1_000_000)
        stats = label_stats(keys, labels)
        assert len(unique) == stats["unique"]
        results[name] = {**stats, "milliseconds_samples": samples,
                         "median_milliseconds": statistics.median(samples)}
    assert results["conservative"]["false_merge_groups"] == 0
    assert results["tracking_only"]["false_merge_groups"] == 0
    assert results["raw_url_set"]["false_merge_groups"] == groups  # bodies/methods
    assert results["proposed"]["false_merge_groups"] == groups  # meaningful fragments
    assert results["proposed_with_w3lib_sort"]["false_merge_groups"] == groups * 3
    assert report["counts"]["proposed_unique"] == results["proposed"]["unique"]
    # Assert repeated-key order/multiplicity and all exact body/method distinctions
    # directly, even though unsafe fragment removal is deliberately demonstrated.
    for policy_to_check in (Policy(), policy, tracking_only):
        assert proposed_url("https://example.test/?k=a&k=b", policy_to_check) != proposed_url("https://example.test/?k=b&k=a", policy_to_check)
        assert proposed_url("https://example.test/?k=a&k=a", policy_to_check) != proposed_url("https://example.test/?k=a", policy_to_check)
        submit_keys = {(r.method, proposed_url(r.url, policy_to_check), r.body) for r in requests if r.url.endswith("/submit")}
        assert len(submit_keys) == groups * 4
    conservative_time = results["conservative"]["median_milliseconds"]
    proposed_time = results["proposed"]["median_milliseconds"]
    return {"synthetic": True, "seed": seed, "entity_groups": groups, "records": len(records),
            "python": platform.python_version(), "platform": platform.platform(), "w3lib": "2.3.1",
            "repeats": repeats, "timing_scope": "key generation + set construction on validated in-memory records; median; no parsing/report I/O",
            "policies": {"tracking_only": asdict(tracking_only), "proposed": asdict(policy), "proposed_with_w3lib_sort": asdict(sorted_policy)},
            "results": results, "fixture_e2e": {"passed": True, "counts": report["counts"], "stderr": completed.stderr.strip()},
            "proposed_time_change_percent_vs_conservative": 100 * (proposed_time - conservative_time) / conservative_time,
            "limitations": "Labels encode invented application semantics. More dedup can include false merges. No real-site gains are established; absolute timings are machine-dependent."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="optional explicit local results file; default stdout")
    args = parser.parse_args()
    result = run()
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
        print(f"Wrote synthetic comparison: {args.output}")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
