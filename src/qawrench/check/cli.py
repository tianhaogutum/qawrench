"""``qawrench check [ARTIFACTS_DIR ...]``: scan CI test artifacts, report what makes the run untrustworthy.

Exit status: 0 no finding at the --fail-on threshold, 1 findings at or above
it, 2 usage or input error (unknown option or check, missing path).
Read-only: no file is written, nothing is executed, no network is used.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys

from .discover import DiscoveryError, discover
from .registry import AREAS, CHECKS, CheckOptions, run_checks, select
from .report import RENDERERS, Run, render_text

PROG = "qawrench check"
FAIL_ON = {"error": "error", "warning": "warning", "never": None}
SIZE = re.compile(r"(?i)\s*([0-9]+(?:\.[0-9]+)?)\s*(b|kb|k|mb|m|gb|g|kib|mib|gib)?\s*")
UNITS = {None: 1, "b": 1, "k": 10 ** 3, "kb": 10 ** 3, "m": 10 ** 6, "mb": 10 ** 6, "g": 10 ** 9, "gb": 10 ** 9,
         "kib": 2 ** 10, "mib": 2 ** 20, "gib": 2 ** 30}

EPILOG = """\
examples:
  qawrench check                                 scan the current directory
  qawrench check test-results/ playwright-report/ lychee.json
  qawrench check test-results/ --format markdown >> "$GITHUB_STEP_SUMMARY"
  qawrench check . --format sarif > qawrench.sarif
  qawrench check . --only flaky,trace-budget --trace-budget 25MB --fail-on warning
  qawrench check --list-checks

exit status: 0 nothing at the --fail-on level, 1 findings at or above it,
2 usage or input error. Nothing is written, executed or sent anywhere.
Docs: https://github.com/tianhaogutum/qawrench/blob/main/docs/check.md
"""


def _names(value: str) -> list[str]:
    names = [name.strip() for name in value.split(",") if name.strip()]
    if not names:
        raise argparse.ArgumentTypeError("expected one or more check names, comma separated")
    return names


def parse_size(value: str) -> int:
    """``500000``, ``500k``, ``25MB`` (decimal) or ``24MiB`` (binary) to bytes."""
    match = SIZE.fullmatch(value)
    if not match:
        raise argparse.ArgumentTypeError("expected a byte count such as 500000, 500kB, 25MB or 24MiB")
    number, unit = match.groups()
    return int(float(number) * UNITS[unit.lower() if unit else None])


def _directory(value: str) -> Path:
    path = Path(value)
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"not a directory: {value}")
    return path


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG, formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG,
        description="Find Playwright reports and traces, HAR files, axe/Pa11y, Lighthouse and lychee JSON "
                    "under ARTIFACTS_DIR and report flaky tests, ambiguous recordings, oversized traces, "
                    "new accessibility violations, noisy performance numbers and broken links.")
    p.add_argument("paths", nargs="*", metavar="ARTIFACTS_DIR",
                   help="artifact folders or files to scan (default: .)")
    p.add_argument("--format", choices=("text", "json", "sarif", "markdown"), default="text",
                   help="report format (default: text); markdown suits a PR comment or $GITHUB_STEP_SUMMARY, "
                        "sarif is SARIF 2.1.0 for code scanning")
    p.add_argument("--fail-on", choices=tuple(FAIL_ON), default="error",
                   help="exit 1 when a finding is at least this severe (default: error)")
    p.add_argument("--only", type=_names, action="append", default=[], metavar="CHECK[,CHECK]",
                   help="run only these checks (repeatable)")
    p.add_argument("--skip", type=_names, action="append", default=[], metavar="CHECK[,CHECK]",
                   help="do not run these checks (repeatable)")
    p.add_argument("--list-checks", action="store_true", help="list the available checks and exit")
    p.add_argument("--trace-budget", type=parse_size, metavar="SIZE",
                   help="warn when a trace.zip is larger than SIZE on disk, e.g. 25MB (default: no budget)")
    p.add_argument("--source-root", type=_directory, metavar="DIR",
                   help="folder lychee ran in, to locate broken links (default: current directory, "
                        "then the report's folder)")
    p.add_argument("--color", choices=("auto", "always", "never"), default="auto",
                   help="colour text output (default: auto; honours NO_COLOR)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="text: show a fix hint under each finding and list clean artifacts")
    return p


def list_checks() -> str:
    width = max(len(check.name) for check in CHECKS) + 2
    lines = ["Checks (select with --only/--skip):"]
    for area in AREAS:
        group = [check for check in CHECKS if check.area == area]
        if not group:
            continue
        lines += ["", f"{area}:"]
        for check in group:
            lines.append(f"  {check.name.ljust(width)}{check.summary}")
            if check.needs_config:
                notes = [f"not run by check: {check.needs_config}"]
            else:
                notes = ["built in" if check.tool == "-" else f"uses: qawrench {check.tool}",
                         f"rules: {', '.join(check.rules)}"]
            if check.extra:
                notes.append(f"needs qawrench[{check.extra}]")
            lines.append(f"  {''.ljust(width)}{'; '.join(notes)}")
    return "\n".join(lines) + "\n"


def _use_color(choice: str) -> bool:
    if choice != "auto":
        return choice == "always"
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.list_checks:
        sys.stdout.write(list_checks())
        return 0
    only = [name for group in args.only for name in group]
    try:
        checks = select(only, [name for group in args.skip for name in group])
        if not checks:
            raise ValueError("--only/--skip leave no check to run")
        options = CheckOptions(trace_budget=args.trace_budget, source_root=args.source_root)
        paths = args.paths or ["."]
        discovery = discover(paths)
    except (ValueError, DiscoveryError) as exc:
        sys.stderr.write(f"{PROG}: error: {exc}\n")
        return 2
    findings = run_checks(discovery, checks, options, requested=only)
    run = Run(paths, discovery.sources, discovery.scanned, checks, findings, FAIL_ON[args.fail_on])
    if args.format == "text":
        output = render_text(run, color=_use_color(args.color), verbose=args.verbose)
    else:
        output = RENDERERS[args.format](run)
    try:
        sys.stdout.write(output)
        sys.stdout.flush()
    except BrokenPipeError:
        pass
    return 1 if run.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
