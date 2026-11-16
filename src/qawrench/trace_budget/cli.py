"""Command line interface. All archive operations are offline."""

import argparse
from dataclasses import fields
import json
import sys

from qawrench import __version__

from .archive import ArchiveError, Limits, compare_archives, ensure_output, inspect_archive, repack_archive, write_report


def _nonnegative(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an integer byte count") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return number


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="qawrench trace-budget", description="Offline Playwright version-8 trace ZIP size budgets and verified lossless repack")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    defaults = Limits()
    for name in ("inspect", "repack", "compare"):
        cmd = commands.add_parser(name)
        cmd.add_argument("archive", help="local trace-shaped ZIP; never fetched or extracted")
        if name == "compare":
            cmd.add_argument("after", help="candidate ZIP; retained payload bytes must match the first ZIP")
            cmd.add_argument("--allow-removals", action="store_true", help="audit omitted members in the loss ledger")
        cmd.add_argument("--out", required=name == "repack", help="new report JSON or repacked ZIP path")
        cmd.add_argument("--force", action="store_true", help="allow atomic replacement of an existing output")
        for field in fields(Limits):
            cmd.add_argument("--" + field.name.replace("_", "-"), type=float if field.name == "max_ratio" else _nonnegative,
                             default=getattr(defaults, field.name), help=f"validation limit (default: {getattr(defaults, field.name)})")
        if name in ("inspect", "compare"):
            cmd.add_argument("--budget-bytes", type=_nonnegative, help="exit 2 if measured bytes exceed this limit")
            cmd.add_argument("--budget-basis", choices=("archive", "stored", "uncompressed"), default="archive")
            if name == "inspect":
                cmd.add_argument("--top", type=_nonnegative, default=10)
        else:
            cmd.add_argument("--level", type=int, choices=range(10), default=9)
            cmd.add_argument("--dry-run", action="store_true", help="validate and report the plan without creating any files")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        limits = Limits(**{field.name: getattr(args, field.name) for field in fields(Limits)})
        if args.out:
            ensure_output(args.archive, args.out, args.force)
            if args.command == "compare":
                ensure_output(args.after, args.out, args.force)
        if args.command == "compare":
            report = compare_archives(args.archive, args.after, limits=limits,
                                      allow_removals=args.allow_removals,
                                      budget_bytes=args.budget_bytes, budget_basis=args.budget_basis)
            if args.out:
                write_report(report, args.out, input_path=args.archive, force=args.force,
                             additional_inputs=(args.after,))
            print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
            return 2 if report["budget"]["exceeded"] else 0
        if args.command == "inspect":
            report = inspect_archive(args.archive, limits=limits, top=args.top,
                                     budget_bytes=args.budget_bytes, budget_basis=args.budget_basis)
            if args.out:
                write_report(report, args.out, input_path=args.archive, force=args.force)
            print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
            return 2 if report["budget"]["exceeded"] else 0
        if args.dry_run:
            inventory = inspect_archive(args.archive, limits=limits, top=0)
            report = {"dry_run": True, "original_bytes": inventory["archive_bytes"],
                      "member_count": inventory["member_count"], "output": args.out,
                      "plan": "ZIP_DEFLATED; normalize metadata; verify member hashes; atomic publish",
                      "estimated_new_bytes": None}
        else:
            report = repack_archive(args.archive, args.out, force=args.force, limits=limits, level=args.level)
        print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
        return 0
    except (ArchiveError, OSError) as exc:
        print(f"qawrench trace-budget: {exc}", file=sys.stderr)
        return 1
