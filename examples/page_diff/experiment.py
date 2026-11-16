"""Sequential, deterministic synthetic comparison; no network and no timing claims."""
from difflib import unified_diff
import json
from pathlib import Path
import sys

# Supports `python examples/page_diff/experiment.py` from a source checkout.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from bs4 import BeautifulSoup
from qawrench.page_diff import compare_files, load_rules


def raw_text(path):
    return BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser").get_text(separator="\n").splitlines()


def matches(change, expected):
    if change["kind"] != expected["kind"] or expected["path_contains"] not in change["path"]:
        return False
    for field in ("old", "new"):
        if field in expected and change[field] != expected[field]:
            return False
        if field + "_text" in expected and expected[field + "_text"] not in json.dumps(change[field]):
            return False
    return True


def run():
    directory = ROOT / "examples" / "page_diff"
    labels = json.loads((directory / "pairs.json").read_text())
    rules = load_rules(directory / "rules.json")
    rows = []
    missed = unexpected = retained = total_expected = 0
    for pair in labels["pairs"]:
        before, after = (directory / pair[k] for k in ("before", "after"))
        lines = list(unified_diff(raw_text(before), raw_text(after), n=0))
        raw_count = sum(line.startswith(("+", "-")) and not line.startswith(("+++", "---")) for line in lines)
        result = compare_files(before, after, rules)
        expected = pair["expected_changes"]
        found = sum(any(matches(change, label) for change in result["changes"]) for label in expected)
        extra = sum(not any(matches(change, label) for label in expected) for change in result["changes"])
        total_expected += len(expected)
        retained += found
        missed += len(expected) - found
        unexpected += extra
        rows.append({"pair": pair["name"], "raw_text_changed_lines": raw_count, "semantic_findings": len(result["changes"]), "expected_substantive_findings": len(expected), "retained": found, "suppressed_differences": len(result["suppressed"])})
    return {"synthetic_only": True, "baseline": "BeautifulSoup get_text(separator='\\n'), splitlines, unified_diff(n=0); count + and - lines, excluding headers", "pairs": rows,
            "totals": {"raw_text_changed_lines": sum(r["raw_text_changed_lines"] for r in rows), "semantic_findings": sum(r["semantic_findings"] for r in rows), "expected_substantive_findings": total_expected, "retained": retained, "missed": missed, "unexpected": unexpected},
            "limitations": "Changed lines and semantic findings have different units. This is neither a changedetection.io benchmark nor evidence of real-site improvement or runtime speedup."}


if __name__ == "__main__":
    report = run()
    print(json.dumps(report, indent=2))
    raise SystemExit(1 if report["totals"]["missed"] or report["totals"]["unexpected"] else 0)
