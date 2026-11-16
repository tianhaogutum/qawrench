"""Local files in, SQLite/JSON/Markdown out. No scripts or URLs are executed."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .adapters import ReportError
from .ledger import LedgerError, import_report, markdown, raw_report, report
from .comparison import compare_runs
from .history import test_history
from .junit import junit_report
from .transfer import export_ledger, import_ledger
from .retention import prune
from .verification import list_archives, list_runs, verify_archive, verify_run
from . import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qawrench flaky", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import", help="validate and atomically import one complete report")
    imp.add_argument("--format", choices=("playwright", "ctrf"), required=True)
    imp.add_argument("--run-id", required=True)
    imp.add_argument("--db", required=True)
    imp.add_argument("--project", help="required project context for CTRF 0.0.29 (one project per import)")
    imp.add_argument("--dry-run", action="store_true", help="validate only; never open/create the database")
    imp.add_argument("input", type=Path)
    rep = sub.add_parser("report", help="read-only history summary")
    rep.add_argument("--db", required=True)
    rep.add_argument("--format", choices=("json", "markdown", "junit"), default="json")
    rep.add_argument("--run-id", action="append", help="select an exact run ID; repeat for multiple runs (default: all)")
    exp = sub.add_parser("export", help="write the exact original report bytes to stdout")
    exp.add_argument("--db", required=True)
    exp.add_argument("--run-id", required=True)
    bundle_exp = sub.add_parser("export-ledger", help="write a deterministic v1 ledger bundle to stdout")
    bundle_exp.add_argument("--db", required=True)
    bundle_exp.add_argument("--run-id", action="append", help="select an exact run ID; repeat for multiple runs (default: all)")
    bundle_imp = sub.add_parser("import-ledger", help="atomically merge a v1 ledger bundle")
    bundle_imp.add_argument("--db", required=True)
    bundle_imp.add_argument("input", type=Path)
    retention = sub.add_parser("prune", help="archive older full runs, retaining auditable historical metrics")
    retention.add_argument("--db", required=True)
    retention.add_argument("--keep-latest", type=int, required=True, help="keep N newest imports; UTC imported_at then run ID descending")
    retention.add_argument("--archive", required=True, type=Path, help="new local v1 bundle path; never overwritten")
    retention.add_argument("--dry-run", action="store_true", help="read-only plan; no export or schema changes")
    verify = sub.add_parser("verify-archive", help="read-only replay audit of an explicitly supplied archive")
    verify.add_argument("--db", required=True)
    verify.add_argument("input", type=Path, help="local v1 bundle path; stored archive paths are never opened")
    verify_full = sub.add_parser("verify-run", help="read-only replay audit of one full local run")
    verify_full.add_argument("--db", required=True)
    verify_full.add_argument("--run-id", required=True, help="exact full run ID; use verify-archive for archived runs")
    inventory = sub.add_parser("list-archives", help="read-only inventory of retained archive references; no bundle audit")
    inventory.add_argument("--db", required=True)
    run_inventory = sub.add_parser("list-runs", help="read-only audit target inventory; no source or bundle verification")
    run_inventory.add_argument("--db", required=True)
    run_inventory.add_argument("--state", choices=("all", "full", "archived"), default="all")
    comparison = sub.add_parser("compare-runs", help="read-only comparison of two exact runs' stored evidence; no replay audit")
    comparison.add_argument("--db", required=True)
    comparison.add_argument("--base-run-id", required=True)
    comparison.add_argument("--run-id", required=True)
    history = sub.add_parser("test-history", help="read-only evidence for one exact fingerprint in caller run order")
    history.add_argument("--db", required=True)
    history.add_argument("--test-id", required=True, help="existing stable test fingerprint: 64 lowercase hex characters")
    history.add_argument("--run-id", action="append", required=True, help="exact run ID; repeat in desired order")
    args = parser.parse_args(argv)
    try:
        if args.command == "import":
            output = import_report(args.db, args.input.read_bytes(), format=args.format,
                                   run_id=args.run_id, project=args.project, dry_run=args.dry_run)
            print(json.dumps(output, sort_keys=True, allow_nan=False))
        elif args.command == "report":
            if args.format == "junit":
                if args.run_id is None or len(args.run_id) != 1:
                    raise LedgerError("report --format junit requires exactly one --run-id")
                print(junit_report(args.db, run_id=args.run_id[0]))
            else:
                data = report(args.db, run_ids=args.run_id)
                print(markdown(data) if args.format == "markdown" else json.dumps(data, indent=2, sort_keys=True, allow_nan=False))
        elif args.command == "export":
            sys.stdout.buffer.write(raw_report(args.db, args.run_id))
        elif args.command == "export-ledger":
            sys.stdout.buffer.write(export_ledger(args.db, run_ids=args.run_id))
        elif args.command == "import-ledger":
            print(json.dumps(import_ledger(args.db, args.input.read_bytes()), sort_keys=True, allow_nan=False))
        elif args.command == "verify-archive":
            print(json.dumps(verify_archive(args.db, args.input), sort_keys=True, allow_nan=False))
        elif args.command == "verify-run":
            print(json.dumps(verify_run(args.db, args.run_id), sort_keys=True, allow_nan=False))
        elif args.command == "list-archives":
            print(json.dumps(list_archives(args.db), sort_keys=True, allow_nan=False))
        elif args.command == "list-runs":
            print(json.dumps(list_runs(args.db, state=args.state), sort_keys=True, allow_nan=False))
        elif args.command == "compare-runs":
            print(json.dumps(compare_runs(args.db, base_run_id=args.base_run_id, run_id=args.run_id), sort_keys=True, allow_nan=False))
        elif args.command == "test-history":
            print(json.dumps(test_history(args.db, test_id=args.test_id, run_ids=args.run_id), sort_keys=True, allow_nan=False))
        elif args.command == "prune":
            print(json.dumps(prune(args.db, keep_latest=args.keep_latest, archive_path=args.archive,
                                   dry_run=args.dry_run), sort_keys=True, allow_nan=False))
        return 0
    except (ReportError, LedgerError, OSError, ValueError) as exc:
        print(f"qawrench flaky: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
