"""Command-line interface. All processing is offline."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from . import __version__
from .adapter import InputError, convert, load_json


def _write_atomic(path: Path, text: str) -> None:
    # Existing output is replaced only after complete input validation and write.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix=".qawrench-links-",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qawrench links", description="Convert a supported Lychee JSON report to SARIF offline.")
    parser.add_argument("report", type=Path)
    mapping = parser.add_mutually_exclusive_group()
    mapping.add_argument("--manifest", type=Path, help="JSON array of exact source/url/line/column occurrences")
    mapping.add_argument("--markdown", action="store_true", help="map supported Markdown inline/reference links and HTTP(S) angle autolinks")
    parser.add_argument("--root", type=Path, default=Path("."), help="root of readable relative source files (default: .)")
    parser.add_argument("--baseline", type=Path, help="prior adapter SARIF; source-root and mapping mode must match")
    parser.add_argument("--source-context", action="store_true", help="include exact complete source lines for mapped locations; may disclose surrounding source")
    parser.add_argument("--out", type=Path, help="atomic output file; default: SARIF on stdout")
    parser.add_argument("--dry-run", action="store_true", help="fully validate and print summary only; write nothing")
    gate = parser.add_mutually_exclusive_group()
    gate.add_argument("--fail-on-findings", action="store_true", help="exit 1 after valid conversion if findings exist")
    gate.add_argument("--fail-on-new", action="store_true", help="require --baseline; exit 1 only if comparison has new results")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    if args.fail_on_new and args.baseline is None:
        parser.error("--fail-on-new requires --baseline")
    try:
        if args.out is not None and args.out.is_symlink():
            raise InputError(f"output {args.out}: symlink outputs are not supported")
        report = load_json(args.report)
        mapping_args = {"manifest": load_json(args.manifest)} if args.manifest else {"markdown": args.markdown}
        if args.baseline is not None:
            mapping_args["baseline"] = load_json(args.baseline)
        sarif = convert(report, root=args.root, **mapping_args, source_context=args.source_context, output_path=args.out,
                        protected_paths=tuple(p for p in (args.report, args.manifest, args.baseline) if p is not None))
        summary = sarif["runs"][0]["properties"]["summary"]
        if not args.dry_run:
            text = json.dumps(sarif, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
            if args.out is None:
                sys.stdout.write(text)
            else:
                _write_atomic(args.out, text)
        print(json.dumps(summary, sort_keys=True), file=sys.stderr)
        if args.fail_on_new:
            return int(summary["baselineComparison"]["new"] > 0)
        return 1 if args.fail_on_findings and summary["resultCount"] else 0
    except (InputError, OSError, UnicodeError, RecursionError) as exc:
        print(f"qawrench links: {exc}", file=sys.stderr)
        return 2
