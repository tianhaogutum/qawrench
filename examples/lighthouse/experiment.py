#!/usr/bin/env python3
"""Seeded, offline synthetic comparison; never launches Lighthouse or Chrome."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import random
import sys
import tempfile

# Supports running this script directly from a source checkout before installation.
_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT), str(_ROOT / "src")]
from examples.lighthouse.fixture_factory import AUDIT, policy, write_runs
from qawrench.lighthouse import compare


def run_experiment(trials: int = 60, seed: int = 37037) -> dict:
    rng = random.Random(seed)
    gate_policy = policy()
    scenarios = []
    counts = {name: Counter() for name in ("single_run", "spread_aware")}
    # All shifted labels describe the generative change, not a significance test.
    families = [("unchanged_noisy", False, 0, 180),
                ("large_shift", True, 450, 70),
                ("small_shift_noisy", True, 140, 180)]
    with tempfile.TemporaryDirectory(prefix="lighthouse-noise-experiment-") as temp:
        root = Path(temp)
        baseline_dir, current_dir = root / "baseline", root / "current"
        for family, shifted, shift, sigma in families:
            for trial in range(trials):
                baseline = [round(max(1, 2000 + rng.gauss(0, sigma)), 6) for _ in range(7)]
                current = [round(max(1, 2000 + shift + rng.gauss(0, sigma)), 6) for _ in range(7)]
                # Independent positive outliers on either side model transient slowness.
                if family != "large_shift":
                    for values in (baseline, current):
                        if rng.random() < 0.25:
                            values[rng.randrange(len(values))] += 1200
                write_runs(baseline_dir, baseline)
                write_runs(current_dir, current)
                gate = compare(baseline_dir, current_dir, gate_policy)
                if gate["decision"] == "invalid":
                    raise RuntimeError(f"experiment input unexpectedly invalid: {gate['errors']}")
                metric = gate["groups"][0]["metrics"][0]
                rule = gate_policy["metrics"][AUDIT]
                boundary = max(rule["absolute_tolerance"], abs(baseline[0]) * rule["relative_tolerance"])
                single = "regression" if current[0] - baseline[0] > boundary else "pass"
                decisions = {"single_run": single, "spread_aware": gate["decision"]}
                for method, decision in decisions.items():
                    counter = counts[method]
                    counter[decision] += 1
                    if shifted:
                        counter["shifted_total"] += 1
                        counter["false_negative"] += decision == "pass"
                        counter["shifted_inconclusive"] += decision == "inconclusive"
                        counter["undetected_shift"] += decision != "regression"
                    else:
                        counter["unchanged_total"] += 1
                        counter["false_positive"] += decision == "regression"
                        counter["unchanged_inconclusive"] += decision == "inconclusive"
                scenarios.append({"id": f"{family}-{trial:03d}", "family": family, "shifted": shifted,
                                  "generative_shift_ms": shift, "sigma_ms": sigma,
                                  "baseline_values": baseline, "current_values": current,
                                  "decisions": decisions, "spread_aware_metric": metric})
        # A separate two-run dataset exercises abstention end-to-end, without stale files.
        low_b, low_c = root / "low-baseline", root / "low-current"
        write_runs(low_b, [2000, 2010])
        write_runs(low_c, [2450, 2460])
        low = compare(low_b, low_c, gate_policy)
    fields = ("pass", "regression", "inconclusive", "false_positive", "false_negative",
              "unchanged_inconclusive", "shifted_inconclusive", "undetected_shift",
              "unchanged_total", "shifted_total")
    return {"synthetic_only": True, "seed": seed, "trials_per_family": trials,
            "scenario_count": len(scenarios), "runs_per_side": 7, "policy": gate_policy,
            "methods": {name: {field: counter[field] for field in fields} for name, counter in counts.items()},
            "single_run_definition": "First run on each side, same max(abs, relative) tolerance, no spread or abstention",
            "false_negative_definition": "Shifted scenario decided pass; inconclusive counted separately",
            "undetected_shift_definition": "Shifted scenario decided pass or inconclusive",
            "limitations": "Synthetic evidence only. No significance, confidence interval, or real-site gain claim. More runs cost more collection work.",
            "low_sample": {"decision": low["decision"], "metric": low["groups"][0]["metrics"][0]},
            "scenarios": scenarios}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="optional full synthetic measurements JSON")
    args = parser.parse_args()
    report = run_experiment()
    if args.out:
        args.out.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    brief = {key: value for key, value in report.items() if key not in ("scenarios", "low_sample")}
    brief["low_sample_decision"] = report["low_sample"]["decision"]
    print(json.dumps(brief, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
