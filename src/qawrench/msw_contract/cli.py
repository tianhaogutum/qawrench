"""Command-line interface for ``qawrench msw-contract``."""
from __future__ import annotations

import argparse
import re
import signal
import sys
import threading

from . import __version__
from ._js import dumps_report, utf8_bytes
from .core import check, format_text, write_report
from .errors import CheckError

PROG = "qawrench msw-contract"
DESCRIPTION = """\
Execute real MSW HTTP handlers, one fresh Node process per explicit request, and check the
actual status, Content-Type and body against OpenAPI. No server required.

Handlers run in a small Node runner bundled with qawrench (Node.js 24 or newer, plus the
project's own msw package); OpenAPI checks and reports are done in Python."""
EPILOG = """\
Exit codes: 0 all examples pass; 1 at least one failed example; 2 invalid input/configuration
(including a missing Node.js or msw). Only explicit reachable branches are checked.
Handlers must be trusted local MSW modules."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, f"{PROG}: {message}. Use --help for usage.\n")


class _Interrupted(BaseException):
    def __init__(self, name: str):
        super().__init__(f"Interrupted by {name}")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog=PROG, usage="%(prog)s --handlers FILE --schema FILE --cases FILE [options]",
                     description=DESCRIPTION, epilog=EPILOG, allow_abbrev=False,
                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--handlers", metavar="FILE", help="MSW handler module (.mjs, .cjs or Node-native .ts)")
    parser.add_argument("--schema", metavar="FILE", help="OpenAPI 3.0.x or 3.1.x document (YAML or JSON)")
    parser.add_argument("--cases", metavar="FILE", help="JSON or YAML array of explicit request cases")
    parser.add_argument("--base-url", metavar="URL",
                        help="resolve relative case URLs and MSW handler patterns against this URL")
    parser.add_argument("--api-root", metavar="PATH", help="strip this API prefix for OpenAPI routing (default /)")
    parser.add_argument("--export", metavar="NAME",
                        help="named handler-array export (default handlers; default export also accepted)")
    parser.add_argument("--timeout", metavar="MS",
                        help="per-case import/execution/body/validation deadline (default 3000; max 120000)")
    parser.add_argument("--max-body-bytes", metavar="N",
                        help="reject larger responses (default 1048576; max 10485760)")
    parser.add_argument("--sample-bytes", metavar="N",
                        help="capture up to N response bytes in reports (default 2048; 0 disables; max 65536)")
    parser.add_argument("--format", metavar="text|json", help="output format (default text)")
    parser.add_argument("--output", metavar="FILE",
                        help="write the report to FILE instead of stdout (atomic replacement, mode 0600)")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def _number(text: str):
    """``Number(text)`` for option values; non-integers are rejected later."""
    stripped = text.strip()
    if not stripped:
        return 0
    for pattern, base in ((r"[+-]?[0-9]+", 10), (r"0[xX][0-9a-fA-F]+", 16), (r"0[oO][0-7]+", 8),
                          (r"0[bB][01]+", 2)):
        if re.fullmatch(pattern, stripped):
            return int(stripped, base) if base == 10 else int(stripped[2:], base)
    if not re.fullmatch(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", stripped):
        return float("nan")
    value = float(stripped)
    return int(value) if value.is_integer() else value


def _write_stdout(text: str) -> None:
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        sys.stdout.write(text)
    else:
        sys.stdout.flush()
        buffer.write(utf8_bytes(text))
        buffer.flush()


def _install_sigterm():
    if threading.current_thread() is not threading.main_thread():
        return None

    def interrupted(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # Let cleanup finish.
        raise _Interrupted("SIGTERM")

    try:
        return signal.signal(signal.SIGTERM, interrupted)
    except (ValueError, OSError):
        return None


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    previous = _install_sigterm()
    try:
        for name in ("handlers", "schema", "cases"):
            if not getattr(args, name):
                raise CheckError(f"Missing --{name}. Use --help for usage.")
        if args.format and args.format not in ("text", "json"):
            raise CheckError("--format must be text or json")
        options = {}
        for flag, key in (("timeout", "timeout_ms"), ("max_body_bytes", "max_body_bytes"),
                          ("sample_bytes", "sample_bytes")):
            if getattr(args, flag) is not None:
                options[key] = _number(getattr(args, flag))
        report = check(handlers_path=args.handlers, schema_path=args.schema, cases_path=args.cases,
                       base_url=args.base_url, api_root="/" if args.api_root is None else args.api_root,
                       export_name="handlers" if args.export is None else args.export, **options)
        output = dumps_report(report) + "\n" if args.format == "json" else format_text(report)
        if args.output:
            write_report(args.output, output)
        else:
            _write_stdout(output)
        return 1 if report["summary"]["failed"] else 0
    except ModuleNotFoundError:
        raise  # The qawrench dispatcher turns a missing optional dependency into an install hint.
    except _Interrupted as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 143
    except KeyboardInterrupt:
        print(f"{PROG}: Interrupted by SIGINT", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - every failure is a one-line error with exit 2
        print(f"{PROG}: {exc}" if str(exc) else f"{PROG}: {type(exc).__name__}", file=sys.stderr)
        return 2
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
