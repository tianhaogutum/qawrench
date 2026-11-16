"""Export, verification and read-only declared-schema preflight commands."""
import argparse
import json
import sys

from qawrench import __version__

from .core import ExportError, export_dataset, verify_export
from .compatibility import (check_schema_compatibility, check_export_schema_compatibility,
                            check_exports_schema_compatibility, check_resume_schema_compatibility,
                            check_export_resume_schema_compatibility,
                            check_resume_export_schema_compatibility)
from .resume import inspect_resume


class _StoreOnce(argparse.Action):
    """Refuse repeated read-only modes while arguments are still being parsed."""

    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest) is not None:
            raise argparse.ArgumentError(self, "may only be specified once")
        setattr(namespace, self.dest, values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qawrench crawlee-export",
                                     description="Strict offline Crawlee JSON to verified Parquet")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("source", nargs="?", help="dataset directory, or JSONL file with --input-format jsonl")
    parser.add_argument("--schema", help="explicit JSON schema (v1 strict; v2 with evolution policy)")
    parser.add_argument("--out", help="new local output; parent directory must exist")
    parser.add_argument("--input-format", choices=("dataset", "jsonl"))
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--batch-bytes", type=int)
    parser.add_argument("--max-item-bytes", type=int)
    parser.add_argument("--resume-dir", help="durable checkpoint directory; reuse with identical inputs/settings")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify", action=_StoreOnce, metavar="PARQUET",
                      help="read-only verification against its sidecar")
    mode.add_argument("--check-schema", nargs=2, action=_StoreOnce, metavar=("OLD", "NEW"),
                      help="read-only schema compatibility preflight before a fresh export")
    mode.add_argument("--check-export-schema", nargs=2, action=_StoreOnce, metavar=("PREVIOUS", "NEW"),
                      help="verify a previous export, then preflight its schema against NEW")
    mode.add_argument("--check-exports-schema", nargs=2, action=_StoreOnce, metavar=("PREVIOUS", "CANDIDATE"),
                      help="verify both exports, then compare PREVIOUS schema to CANDIDATE")
    mode.add_argument("--check-resume-schema", nargs=2, action=_StoreOnce, metavar=("DIRECTORY", "NEW"),
                      help="validate a checkpoint, then preflight its bound schema against NEW")
    mode.add_argument("--check-export-resume-schema", nargs=2, action=_StoreOnce,
                      metavar=("PREVIOUS", "DIRECTORY"),
                      help="verify PREVIOUS, inspect a checkpoint, then compare their schemas")
    mode.add_argument("--check-resume-export-schema", nargs=2, action=_StoreOnce,
                      metavar=("DIRECTORY", "CANDIDATE"),
                      help="inspect a checkpoint, verify CANDIDATE, then compare their schemas")
    mode.add_argument("--inspect-resume", action=_StoreOnce, metavar="DIRECTORY",
                      help="read-only validation and progress report for an existing checkpoint")
    args = parser.parse_args(argv)
    # Validate after parsing so help/malformed/duplicate/conflicting modes keep
    # argparse precedence. An explicit empty mode must never select export.
    for flag, value in (("--verify", args.verify), ("--inspect-resume", args.inspect_resume)):
        if value is not None and value == "":
            parser.error(f"argument {flag}: must not be empty")
    for flag, paths in (("--check-schema", args.check_schema),
                        ("--check-export-schema", args.check_export_schema),
                        ("--check-exports-schema", args.check_exports_schema),
                        ("--check-resume-schema", args.check_resume_schema),
                        ("--check-export-resume-schema", args.check_export_resume_schema),
                        ("--check-resume-export-schema", args.check_resume_export_schema)):
        if paths is not None and "" in paths:
            parser.error(f"argument {flag}: paths must not be empty")
    comparison = (args.check_schema or args.check_export_schema or args.check_exports_schema
                  or args.check_resume_schema or args.check_export_resume_schema
                  or args.check_resume_export_schema)
    if comparison or args.inspect_resume:
        if any(value is not None for value in (
                args.source, args.schema, args.out, args.resume_dir, args.input_format,
                args.batch_size, args.batch_bytes, args.max_item_bytes)):
            if args.check_resume_export_schema:
                parser.error("--check-resume-export-schema accepts only DIRECTORY and CANDIDATE paths")
            if args.check_export_resume_schema:
                parser.error("--check-export-resume-schema accepts only PREVIOUS and DIRECTORY paths")
            if args.check_exports_schema:
                parser.error("--check-exports-schema accepts only PREVIOUS and CANDIDATE paths")
            if args.check_resume_schema:
                parser.error("--check-resume-schema accepts only DIRECTORY and NEW paths")
            if args.check_export_schema:
                parser.error("--check-export-schema accepts only PREVIOUS and NEW paths")
            parser.error("--inspect-resume accepts only a checkpoint directory" if args.inspect_resume
                         else "--check-schema accepts only OLD and NEW schema paths")
    elif args.verify:
        if any(value is not None for value in (
                args.source, args.schema, args.out, args.resume_dir)):
            parser.error("--verify cannot be combined with source, --schema, --out or --resume-dir")
        if any(value is not None for value in (
                args.input_format, args.batch_bytes, args.max_item_bytes)):
            parser.error("--verify accepts only PARQUET and optional --batch-size")
    elif not (args.source and args.schema and args.out):
        parser.error("export requires source, --schema and --out")
    # Preserve export/verify defaults while detecting explicitly supplied options
    # that have no meaning in the selected read-only mode.
    args.input_format = args.input_format if args.input_format is not None else "dataset"
    args.batch_size = args.batch_size if args.batch_size is not None else 1000
    args.batch_bytes = args.batch_bytes if args.batch_bytes is not None else 16 * 1024 * 1024
    args.max_item_bytes = args.max_item_bytes if args.max_item_bytes is not None else 8 * 1024 * 1024
    try:
        if args.inspect_resume:
            result = inspect_resume(args.inspect_resume)
        elif args.check_resume_export_schema:
            result = check_resume_export_schema_compatibility(*args.check_resume_export_schema)
        elif args.check_export_resume_schema:
            result = check_export_resume_schema_compatibility(*args.check_export_resume_schema)
        elif args.check_resume_schema:
            result = check_resume_schema_compatibility(*args.check_resume_schema)
        elif args.check_schema:
            result = check_schema_compatibility(*args.check_schema)
        elif args.check_export_schema:
            result = check_export_schema_compatibility(*args.check_export_schema)
        elif args.check_exports_schema:
            result = check_exports_schema_compatibility(*args.check_exports_schema)
        elif args.verify:
            result = verify_export(args.verify, batch_size=args.batch_size)
        else:
            result = export_dataset(args.source, schema=args.schema, out=args.out,
                                    input_format=args.input_format, batch_size=args.batch_size,
                                    batch_bytes=args.batch_bytes, max_item_bytes=args.max_item_bytes,
                                    resume_dir=args.resume_dir)
    except ExportError as exc:
        print(json.dumps({"error": str(exc), "rows_validated": exc.rows_validated,
                          "rows_committed": 0}, ensure_ascii=True), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('{"error":"interrupted","rows_committed":0}', file=sys.stderr)
        return 130
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 2 if comparison and not result["compatible"] else 0
