"""Command line entry point; input is always local, inert UTF-8 data."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from qawrench import __version__

from .core import ContractError, compare_files, load_rules


def markdown(report):
    # JSON strings on separate list entries preserve multiline values and escape HTML.
    def value(obj):
        return json.dumps(obj, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("`", "\\u0060")

    lines = ["# Stable page diff", "", f"Status: **{report['status']}**", "",
             f"Semantic findings: {len(report['changes'])}. Suppressed differences: {len(report['suppressed'])}.", "",
             "## Semantic changes", ""]
    for item in report["changes"]:
        lines.extend([f"- {item['kind']} `{value(item['path'])}`", f"  - old: `{value(item['old'])}`", f"  - new: `{value(item['new'])}`"])
        if "before_path" in item:
            lines.extend([f"  - before location: `{value(item['before_path'])}`", f"  - after location: `{value(item['after_path'])}`"])
        if "identity" in item:
            lines.append(f"  - identity: `{value(item['identity'])}`")
        if "reason" in item:
            lines.append(f"  - reason: `{value(item['reason'])}`")
    if not report["changes"]:
        lines.append("No semantic changes in the configured region.")
    lines.extend(["", "## Suppressed differences", ""])
    for item in report["suppressed"]:
        lines.extend([f"- {item['rule']} `{value(item['path'])}`", f"  - old: `{value(item['old'])}`", f"  - new: `{value(item['new'])}`", f"  - normalized: `{value(item['normalized'])}`"])
        if "before_path" in item:
            lines.extend([f"  - before location: `{value(item['before_path'])}`", f"  - after location: `{value(item['after_path'])}`"])
        if "before_rules" in item:
            lines.extend([f"  - before rules: `{value(item['before_rules'])}`", f"  - after rules: `{value(item['after_rules'])}`"])
        if "before_matched_nodes" in item:
            lines.extend([f"  - before matched nodes: `{value(item['before_matched_nodes'])}`",
                          f"  - after matched nodes: `{value(item['after_matched_nodes'])}`"])
        if "before_target_path" in item:
            lines.extend([f"  - before target location: `{value(item['before_target_path'])}`",
                          f"  - after target location: `{value(item['after_target_path'])}`"])
    if not report["suppressed"]:
        lines.append("None.")
    lines.extend(["", "## Normalization rule coverage", "", "```json", value(report.get("rule_coverage", {})), "```"])
    lines.extend(["", "## Full normalization audit", "", "```json", value(report["normalizations"]), "```", "", "Only the required region is compared. Values describe the parsed DOM, not rendering.", ""])
    return "\n".join(lines)


def _write(path, content):
    destination = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, prefix=".stable-diff-", delete=False) as handle:
            temporary = handle.name
            handle.write(content)
        os.replace(temporary, destination)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="qawrench page-diff", description="Compare two local HTML snapshots without fetching URLs or executing scripts.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--rules", required=True, type=Path)
    parser.add_argument("--out", type=Path, help="atomically write report (default: stdout); parent directory must exist")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--fail-on-change", action="store_true", help="exit 1 for semantic findings; invalid input exits 2")
    parser.add_argument("--dry-run", action="store_true", help="compare and report to stdout, never write --out")
    args = parser.parse_args(argv)
    try:
        if args.out and args.out.resolve() in {p.resolve() for p in (args.before, args.after, args.rules)}:
            raise ContractError("--out must not overwrite an input or rules file")
        # Resolve does not identify hard links, so check samefile for existing outputs too.
        if args.out and args.out.exists() and any(args.out.samefile(p) for p in (args.before, args.after, args.rules) if p.exists()):
            raise ContractError("--out must not overwrite a hard-linked input")
        result = compare_files(args.before, args.after, load_rules(args.rules))
        content = json.dumps(result, indent=2, ensure_ascii=True) + "\n" if args.format == "json" else markdown(result)
        if args.out and not args.dry_run:
            _write(args.out, content)
        else:
            sys.stdout.write(content)
        return 1 if args.fail_on_change and result["changes"] else 0
    except (ContractError, OSError) as exc:
        print(f"qawrench page-diff: {exc}", file=sys.stderr)
        return 2
