"""Local-file CLI. Dry runs emit only stdout and never write output files."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from qawrench import __version__

from .core import AuditError, Policy, audit, json_text, load_requests, strict_json
from .checkpoint import BatchConflict, Checkpoint, validate_batch_id
from .robots import RobotsPolicy


def atomic_write(path: Path, content: str) -> None:
    """Replace one output atomically; never leave a partly written JSON file."""
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as handle:
            name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if name is not None and os.path.exists(name):
            os.unlink(name)


def check_paths(inputs: list[Path], outputs: list[Path]) -> None:
    for i, output in enumerate(outputs):
        for other in inputs + outputs[:i]:
            if output.resolve() == other.resolve() or (
                output.exists() and other.exists() and os.path.samefile(output, other)
            ):
                raise AuditError("all input and output paths must use distinct files")
        if output.is_symlink():
            raise AuditError(f"output must not be a symlink: {output}")
        if not output.parent.is_dir():
            raise AuditError(f"output directory does not exist: {output.parent}")
        if output.exists() and not output.is_file():
            raise AuditError(f"output must be a regular file: {output}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "verify-witnesses":
        from .verify_cli import main as verify_main
        return verify_main(argv[1:])
    parser = argparse.ArgumentParser(
        prog="qawrench frontier",
        description="Offline explicit-policy request dedup audit; never fetches URLs",
        epilog="Also: 'qawrench frontier verify-witnesses REPORT --robots MANIFEST [--verify-reason]' "
               "verifies saved strict robots witnesses in a report against local snapshots "
               "(see 'qawrench frontier verify-witnesses --help').")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("input", type=Path, nargs="?", help="local JSONL requests, or Crawlee JSON sources array")
    parser.add_argument("--format", choices=("jsonl", "crawlee"), help="incoming batch format; default: jsonl")
    parser.add_argument("--policy", type=Path, help="version-1 policy JSON; omitted = checkpoint policy or exact default")
    parser.add_argument("--checkpoint-in", type=Path, help="local version-1/2 checkpoint history to merge or re-audit")
    parser.add_argument("--checkpoint-only", action="store_true",
                        help="re-audit --checkpoint-in without an incoming batch; no input/--batch-id/--format")
    parser.add_argument("--checkpoint-out", type=Path, help="save accepted policy and original batch history")
    parser.add_argument("--batch-id", help="stable ID for exact format/text replay; requires checkpoint-in/out")
    parser.add_argument("--robots", type=Path, help="local version-1 inline robots snapshot manifest; never fetched")
    parser.add_argument("--user-agent", help="explicit robots product token; required with --robots")
    parser.add_argument("--max-robots-bytes", type=int,
                        help="positive per-available-snapshot UTF-8 byte cap; requires --robots/--user-agent")
    parser.add_argument("--enforce-robots", action="store_true",
                        help="require --robots/--user-agent and reject any disallow/unknown before emission")
    parser.add_argument("--strict-robots", action="store_true",
                        help="require --enforce-robots and reject malformed-rule diagnostics; extensions stay ignored")
    parser.add_argument("--frontier", type=Path, help="representative originals as normalized request JSONL")
    parser.add_argument("--report", type=Path, help="complete audit JSON including every collision witness")
    parser.add_argument("--on-invalid", choices=("fail", "skip"), default="fail")
    parser.add_argument("--dry-run", action="store_true", help="print audit to stdout, write no files")
    args = parser.parse_args(argv)
    if args.checkpoint_only:
        if args.checkpoint_in is None:
            parser.error("--checkpoint-only requires --checkpoint-in")
        if args.input is not None:
            parser.error("--checkpoint-only cannot be combined with input")
        if args.batch_id is not None:
            parser.error("--checkpoint-only cannot be combined with --batch-id")
        if args.format is not None:
            parser.error("--checkpoint-only cannot be combined with --format")
    elif args.input is None:
        parser.error("input is required unless --checkpoint-only is used")
    if not args.dry_run and (args.frontier is None or args.report is None):
        parser.error("--frontier and --report are required unless --dry-run is used")
    try:
        if args.max_robots_bytes is not None:
            if args.max_robots_bytes < 1:
                raise AuditError("--max-robots-bytes must be a positive integer")
            if args.robots is None:
                raise AuditError("--max-robots-bytes requires --robots and --user-agent")
        validate_batch_id(args.batch_id)
        if args.batch_id is not None and not (args.checkpoint_in or args.checkpoint_out):
            raise AuditError("--batch-id requires --checkpoint-in or --checkpoint-out")
        if (args.robots is None) != (args.user_agent is None):
            raise AuditError("--robots and --user-agent must be supplied together")
        if args.enforce_robots and args.robots is None:
            raise AuditError("--enforce-robots requires --robots and --user-agent")
        if args.strict_robots and not args.enforce_robots:
            raise AuditError("--strict-robots requires --enforce-robots")
        if not args.dry_run:
            check_paths([p for p in (args.input, args.policy, args.robots, args.checkpoint_in) if p is not None],
                        [args.frontier, args.report] + ([args.checkpoint_out] if args.checkpoint_out else []))
        previous = (Checkpoint.from_dict(strict_json(args.checkpoint_in.read_text(encoding="utf-8")))
                    if args.checkpoint_in else None)
        policy = (Policy.from_dict(strict_json(args.policy.read_text(encoding="utf-8"))) if args.policy
                  else previous.policy if previous else Policy())
        robots = (RobotsPolicy.from_dict(strict_json(args.robots.read_text(encoding="utf-8")),
                                        max_robots_bytes=args.max_robots_bytes) if args.robots else None)
        checkpoint = None
        conflicts = previous.policy_conflicts(policy) if previous else []
        batch_conflicts = []
        batch_status = "appended"
        if args.checkpoint_in or args.checkpoint_out:
            base = previous or Checkpoint(policy)
            if args.checkpoint_only:
                checkpoint = base
                batch_status = "not_supplied"
            else:
                try:
                    # Preserve line endings: batch identity is exact UTF-8 text.
                    checkpoint = base.append(args.input.read_bytes().decode("utf-8"), args.format or "jsonl",
                                             batch_id=args.batch_id)
                    if checkpoint is base:
                        batch_status = "replayed"
                except BatchConflict as exc:
                    batch_conflicts.append(exc.conflict)
                    batch_status = "conflict"
                    checkpoint = base
            requests, issues, total = checkpoint.load_requests()
        else:
            requests, issues, total = load_requests(args.input, args.format or "jsonl")
        frontier, report = audit(requests, policy, issues=issues, total=total, on_invalid=args.on_invalid,
                                 robots=robots, user_agent=args.user_agent, enforce_robots=args.enforce_robots,
                                 strict_robots=args.strict_robots)
        if checkpoint is not None:
            report["checkpoint"] = {"version": checkpoint.version, "batches": len(checkpoint.batches),
                                    "previous_batches": len(previous.batches) if previous else 0,
                                    "policy_conflicts": conflicts, "batch_conflicts": batch_conflicts,
                                    "incoming_batch": {"batch_id": args.batch_id, "status": batch_status},
                                    "emitted": False}
        if batch_conflicts:
            report["errors"].append(f"checkpoint batch ID conflict: {args.batch_id}")
            report["status"] = "rejected"
        if conflicts:
            report["errors"].append("checkpoint dedupe policy conflicts: " + ", ".join(c["field"] for c in conflicts))
            report["status"] = "rejected"
        if args.dry_run:
            print(json.dumps(report, indent=2, ensure_ascii=True, sort_keys=True))
        else:
            if not report["errors"]:
                atomic_write(args.frontier, "".join(json_text(record) + "\n" for record in frontier))
                report["frontier_emitted"] = True
                if args.checkpoint_out:
                    atomic_write(args.checkpoint_out, json_text(checkpoint.to_dict()) + "\n")
                    report["checkpoint"]["emitted"] = True
            atomic_write(args.report, json.dumps(report, indent=2, ensure_ascii=True, sort_keys=True) + "\n")
        for warning in report["warnings"]:
            print(f"warning: {warning}", file=sys.stderr)
        if report["errors"]:
            for error in report["errors"]:
                print(f"error: {error}", file=sys.stderr)
            for issue in issues:
                print(f"error: {issue['location']}: {issue['error']}", file=sys.stderr)
            return 2
        if not args.dry_run:
            print(json_text({"status": "ok", "counts": report["counts"]}))
        return 0
    except (AuditError, OSError, UnicodeError, RecursionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
