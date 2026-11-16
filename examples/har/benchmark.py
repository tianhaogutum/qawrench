"""Reproducible offline comparison. Prints measurements; writes no persistent files.

Run from the qawrench checkout root: ``uv run python examples/har/benchmark.py``.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from time import perf_counter

# Make the documented checkout command find examples.har and the src/ package.
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from examples.har.fixtures import RULES, entry, har
from qawrench.har import DoctorError, build_plan, request_key, rewrite_har


def run():
    entries, intended = [], []
    for i in range(1000):
        identity = i % 900  # 100 benign duplicate groups after normalization.
        method = "POST" if identity % 2 else "GET"
        body = '{"id":%d}' % identity if method == "POST" else None
        suffix = f"/items/{identity}?tag=red&tag=blue&q=a%20b&empty="
        e = entry("https://stage.example" + suffix + f"&cacheBust={i}", method, body,
                  response_text='{"id":%d}' % identity, binary=identity % 5 == 0)
        entries.append(e)
        # Independently specified target, without invoking the rewrite algorithm.
        intended.append(("http://fixture.test" + suffix, method, body.encode("utf-8") if body is not None else b""))
    source = har(entries)
    before = {request_key(e["request"]) for e in source["log"]["entries"]}
    start = perf_counter()
    plan = build_plan(source, RULES)
    plan_ms = (perf_counter() - start) * 1000
    start = perf_counter()
    rewritten, _ = rewrite_har(source, RULES)
    rewrite_ms = (perf_counter() - start) * 1000
    after = {request_key(e["request"]) for e in rewritten["log"]["entries"]}
    before_mismatches = sum(key not in before for key in intended)
    after_mismatches = sum(key not in after for key in intended)
    assert (before_mismatches, after_mismatches) == (1000, 0)
    assert plan["summary"]["collisionGroups"] == 100 and plan["safeToApply"]
    for old, new, expected in zip(source["log"]["entries"], rewritten["log"]["entries"], intended):
        assert request_key(new["request"]) == expected
        restored = copy.deepcopy(new)
        restored["request"]["url"] = old["request"]["url"]
        restored["request"]["queryString"] = old["request"]["queryString"]
        assert restored == old, "unexpected change outside URL/query metadata"
    second, second_plan = rewrite_har(rewritten, RULES)
    assert second == rewritten and second_plan["summary"]["changedEntries"] == 0
    assert json.loads(json.dumps(rewritten)) == rewritten
    # Exercise both actual CLI commands, standard JSON reading, no external service.
    with tempfile.TemporaryDirectory(prefix="har-doctor-benchmark-") as directory:
        folder = Path(directory)
        input_path, rules_path = folder / "synthetic.har", folder / "rules.json"
        input_path.write_text(json.dumps(source), encoding="utf-8")
        original = input_path.read_bytes()
        rules_path.write_text(json.dumps(RULES), encoding="utf-8")
        start = perf_counter()
        for command, name in (("plan", "plan.json"), ("apply", "rewritten.har")):
            completed = subprocess.run([sys.executable, "-m", "qawrench.har", command, str(input_path),
                                        "--rules", str(rules_path), "--out", str(folder / name)],
                                       check=True, capture_output=True, text=True)
            json.loads(completed.stdout)
        cli_ms = (perf_counter() - start) * 1000
        assert json.loads((folder / "rewritten.har").read_text()) == rewritten
        assert json.loads((folder / "plan.json").read_text()) == plan
        assert input_path.read_bytes() == original
    conflict_source = copy.deepcopy(source)
    conflict_source["log"]["entries"][900]["response"]["status"] = 201
    conflict_plan = build_plan(conflict_source, RULES)
    assert conflict_plan["summary"]["conflictingGroups"] == 1
    try:
        rewrite_har(conflict_source, RULES)
    except DoctorError:
        pass
    else:
        raise AssertionError("conflicting response did not block apply")
    return {"fixture": "synthetic offline; not evidence of real-site improvement", "entries": 1000,
            "intendedMatchesBefore": 1000 - before_mismatches, "intendedMatchesAfter": 1000 - after_mismatches,
            "exactKeyMismatchesBefore": before_mismatches, "exactKeyMismatchesAfter": after_mismatches,
            "collisionGroups": plan["summary"]["collisionGroups"], "conflictingGroups": 0,
            "injectedConflictGroups": conflict_plan["summary"]["conflictingGroups"],
            "payloadAndUntouchedFieldPreservation": True, "repeatApplyChangedEntries": 0,
            "timingMs": {"plan": round(plan_ms, 3), "rewriteIncludingReplan": round(rewrite_ms, 3),
                         "twoCLIProcesses": round(cli_ms, 3)},
            "timingScope": "single local run, sequential; no replay-speed comparison or promised speedup"}


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
