"""Read local statewatch artifacts without importing the test suite.

``qawrench statewatch`` reads what ``python -m pytest --statewatch POLICY.json``
wrote (``report.json``, ``events.jsonl``) and converts pytest-threadlanes and
flakedoctor reports into a policy or a pytest command. It uses only the
standard library; recording needs the pytest plugin and the ``statewatch`` extra.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from . import __version__

PROG = "qawrench statewatch"
DEFAULT_REPORT = Path(".statewatch/report.json")

EPILOG = """\
record first (needs qawrench[statewatch]):
  python -m pytest --statewatch policy.json [--statewatch-confirm]

examples:
  qawrench statewatch                                 summarise .statewatch/report.json
  qawrench statewatch --events .statewatch/events.jsonl --limit 100
  qawrench statewatch --from-lanes shared.json --module my_app.settings > policy.json
  qawrench statewatch --from-doctor doctor.json --policy policy.json
"""


def validate_report(report: object) -> dict:
    """Return ``report`` if it has the shape of a schema-version-1 ``report.json``.

    Raises ValueError otherwise. Only the fields this command and ``qawrench
    check`` read are checked; nothing is repaired or guessed.
    """
    if (
        not isinstance(report, dict)
        or report.get("schema_version") != 1
        or not isinstance(report.get("tests"), list)
    ):
        raise ValueError("not a statewatch v1 report")
    expected = {
        "tests_with_net_changes": int,
        "status": str,
        "exit_code": int,
        "first_observed_writers": dict,
        "observer_errors": list,
        "confirmations": list,
        "events_file": str,
    }
    for key, kind in expected.items():
        if type(report.get(key)) is not kind:
            raise ValueError(f"statewatch v1 report field {key!r} is missing or not a {kind.__name__}")
    if any(not isinstance(test, dict) or not isinstance(test.get("nodeid"), str) for test in report["tests"]):
        raise ValueError("statewatch v1 report has a test entry without a nodeid")
    return report


def show_events(path: Path, limit: int) -> int:
    try:
        count = 0
        with path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                try:
                    event = json.loads(line)
                except ValueError as exc:
                    if not line.endswith("\n"):
                        print(
                            f"{PROG}: incomplete final event at line {number} ignored",
                            file=sys.stderr,
                        )
                        break
                    raise ValueError(f"invalid event at line {number}") from exc
                if not isinstance(event, dict) or not isinstance(event.get("nodeid"), str):
                    raise ValueError(f"invalid event object at line {number}")
                fields = [
                    change["field"] for change in event["net_changes"] if not change["allowed"]
                ]
                print(f"{event['nodeid']}: {', '.join(fields) or 'no net changes'}")
                count += 1
                if count >= limit:
                    print(f"Stopped after {limit} completed events (--limit to change).")
                    break
        return 0
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        print(f"{PROG}: cannot read {path}: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Read the local reports of the statewatch pytest plugin, or turn "
        "pytest-threadlanes / flakedoctor reports into a statewatch policy or command.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    parser.add_argument(
        "report", nargs="?", type=Path, help=f"report.json to summarise (default: {DEFAULT_REPORT})"
    )
    parser.add_argument("--events", type=Path, help="Read a crash-recoverable event stream")
    parser.add_argument(
        "--from-lanes", type=Path, help="Generate policy JSON from a threadlanes report"
    )
    parser.add_argument(
        "--from-doctor", type=Path, help="Print a value-tracing command for a doctor order repro"
    )
    parser.add_argument("--policy", type=Path, help="Observation policy for --from-doctor")
    parser.add_argument(
        "--module",
        action="append",
        default=[],
        help="Module prefix to select discovered globals (repeatable)",
    )
    parser.add_argument("--limit", type=int, default=20, help="Maximum events to display")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    if args.from_doctor is not None:
        if args.report is not None or args.events is not None or args.from_lanes is not None:
            parser.error("--from-doctor cannot be combined with another input mode")
        if args.policy is None:
            parser.error("--from-doctor requires --policy")
        from .adapters import doctor_argv
        from .policy import Policy

        try:
            Policy.load(args.policy)
            print(shlex.join(doctor_argv(args.from_doctor, args.policy)))
        except (OSError, UnicodeError, ValueError) as exc:
            print(f"{PROG}: cannot import doctor report: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.from_lanes is not None:
        if args.report is not None or args.events is not None:
            parser.error("--from-lanes cannot be combined with report/event display")
        from .adapters import policy_from_lanes

        try:
            policy, unmapped, truncated = policy_from_lanes(args.from_lanes, args.module)
            if not policy["env"] and not policy["cwd"] and not policy["variables"]:
                raise ValueError(
                    "no supported fields mapped; pass --module for application globals"
                )
        except (OSError, UnicodeError, ValueError) as exc:
            print(f"{PROG}: cannot import discovery report: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(policy, ensure_ascii=True, indent=2))
        if truncated:
            print(
                f"{PROG}: source discovery was truncated; this policy is partial",
                file=sys.stderr,
            )
        if unmapped:
            print(
                f"{PROG}: {len(unmapped)} paths were not mapped; "
                "pass explicit --module prefixes for globals. "
                "Plugin objects, signals and non-root loggers are outside this adapter.",
                file=sys.stderr,
            )
        return 0
    if args.events is not None:
        if args.report is not None:
            parser.error("use a report or --events, not both")
        if args.limit < 1:
            parser.error("--limit must be positive")
        return show_events(args.events, args.limit)
    args.report = args.report or DEFAULT_REPORT
    try:
        report = validate_report(json.loads(args.report.read_text(encoding="utf-8")))
        print(
            f"{len(report['tests'])} protocols; {report['tests_with_net_changes']} with net changes"
        )
        print(f"Status: {report['status']} (pytest exit {report['exit_code']})")
        for field, writer in report["first_observed_writers"].items():
            print(f"{field}: first observed writer {writer}")
        for result in report.get("confirmations", []):
            print(f"{result['victim']}: {result['status']}")
            for pair in result["pairs"]:
                print(f"  {pair['writer']} -> {result['victim']}: {pair['status']}")
                print(f"  {pair['command']}")
        for error in report.get("observer_errors", []):
            print(f"incomplete: {error['field']}: {error['reason']}")
        print(f"Details: {report['events_file']}")
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        print(f"{PROG}: cannot read {args.report}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
