"""CLI: only local JSON reads and explicitly requested report writes."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import uuid

from .core import compare, load_json, validate_policy
from .baseline import create_baseline
from .report import markdown
from . import __version__


def _atomic_write(path: Path, text: str) -> None:
    # os.open applies the process umask; NamedTemporaryFile would leave reports private (0600).
    temp = path.with_name(f".noise-gate-{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _outputs(args: argparse.Namespace) -> list[Path]:
    paths = [Path(p).resolve() for p in (args.out, args.markdown) if p]
    return _validate_outputs(paths, [args.baseline, args.current], args.config)


def _validate_outputs(paths: list[Path], inputs: list[str], config_path: str) -> list[Path]:
    if len(set(paths)) != len(paths):
        raise ValueError("JSON and Markdown outputs must be different files")
    roots = [Path(p).resolve() for p in inputs]
    config = Path(config_path).resolve()
    for path in paths:
        if path == config or any(path == root or path.is_relative_to(root) for root in roots):
            raise ValueError("output must be outside input directories and must not overwrite an input file or the config")
        if not path.parent.is_dir() or path.is_dir():
            raise ValueError(f"output parent must exist and output must be a file: {path}")
    return paths


def _invalid(exc: Exception) -> int:
    print(json.dumps({"schema_version": 1, "decision": "invalid", "exit_code": 2,
                      "errors": [str(exc)]}, ensure_ascii=True, allow_nan=False), file=sys.stderr)
    return 2


def _save_baseline(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="qawrench lighthouse save-baseline",
                                     description="Save a self-contained, policy-bound baseline manifest")
    parser.add_argument("baseline", help="directory containing immediate *.json baseline LHRs")
    parser.add_argument("--config", required=True, help="strict JSON policy to pin in the manifest")
    parser.add_argument("--out", help="manifest path; default prints JSON to stdout")
    parser.add_argument("--dry-run", action="store_true", help="validate and print manifest; write nothing")
    args = parser.parse_args(argv)
    try:
        if not args.dry_run:
            _validate_outputs([Path(args.out).resolve()] if args.out else [], [args.baseline], args.config)
        manifest = create_baseline(args.baseline, load_json(args.config))
        rendered = json.dumps(manifest, indent=2, ensure_ascii=True, sort_keys=True, allow_nan=False) + "\n"
        if args.out and not args.dry_run:
            _atomic_write(Path(args.out).resolve(), rendered)
        else:
            sys.stdout.write(rendered)
    except (OSError, ValueError, RecursionError) as exc:
        return _invalid(exc)
    action = "validated" if args.dry_run else "saved"
    print(f"qawrench lighthouse: {action} baseline ({len(manifest['runs'])} runs)", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["save-baseline"]:
        return _save_baseline(argv[1:])
    parser = argparse.ArgumentParser(prog="qawrench lighthouse", description="Offline Lighthouse 12.8.2 numeric-audit regression gate",
        epilog="Save a baseline: qawrench lighthouse save-baseline DIR --config POLICY --out MANIFEST")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("baseline", help="directory containing immediate *.json baseline LHRs, or a saved baseline manifest")
    parser.add_argument("current", help="directory containing only immediate *.json current LHRs")
    parser.add_argument("--config", required=True, help="strict JSON policy (see examples/lighthouse/gate.json)")
    parser.add_argument("--out", help="JSON report path; default prints JSON to stdout")
    parser.add_argument("--markdown", help="optional complete Markdown report path")
    parser.add_argument("--markdown-summary", action="store_true",
                        help="omit embedded JSON evidence from Markdown; requires --markdown; JSON report stays complete")
    parser.add_argument("--confidence-level", type=float,
                        help="exact median-change confidence level, e.g. 0.95; diagnostic unless --confidence-gate is set")
    parser.add_argument("--confidence-gate", action="store_true",
                        help="use confidence intervals for decisions; requires --confidence-level and relative_tolerance=0 for every metric")
    parser.add_argument("--mismatch-limit", type=int,
                        help="positive maximum diagnostic candidate pairs; reports total/omitted counts, without changing decisions")
    parser.add_argument("--mismatch-field-limit", type=int,
                        help="positive maximum change records per diagnostic candidate; reports per-pair total/omitted counts")
    parser.add_argument("--dry-run", action="store_true", help="compare and print JSON; ignore output paths and write nothing")
    args = parser.parse_args(argv)
    if args.markdown_summary and not args.markdown:
        parser.error("--markdown-summary requires --markdown")
    try:
        if not args.dry_run:
            _outputs(args)
        policy = validate_policy(load_json(args.config))
        report = compare(args.baseline, args.current, policy, confidence_level=args.confidence_level,
                         confidence_gate=args.confidence_gate, mismatch_limit=args.mismatch_limit,
                         mismatch_field_limit=args.mismatch_field_limit)
    except (OSError, ValueError, RecursionError) as exc:
        # No output is written for config/argument failures, preventing unsafe paths.
        return _invalid(exc)
    rendered = json.dumps(report, indent=2, ensure_ascii=True, sort_keys=True, allow_nan=False) + "\n"
    try:
        if args.out and not args.dry_run:
            _atomic_write(Path(args.out).resolve(), rendered)
        else:
            sys.stdout.write(rendered)
        if args.markdown and not args.dry_run:
            _atomic_write(Path(args.markdown).resolve(), markdown(report, include_evidence=not args.markdown_summary))
    except OSError as exc:
        print(f"qawrench lighthouse: cannot write report: {exc}", file=sys.stderr)
        return 2
    print(f"qawrench lighthouse: {report['decision']} (exit {report['exit_code']})", file=sys.stderr)
    return report["exit_code"]
