"""Command-line interface. No network or script execution paths."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import (DoctorError, __version__, build_plan, build_preflight, embed_form_params,
               export_inventory, load_json, rewrite_har)
from .core import atomic_write_json
from .fixtures import load_fixture


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qawrench har", description="Select HAR fixtures and plan explicit request rewrites offline; apply only collision-free rules.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export-inventory", help="copy distinct exact requests from a HAR into an inventory without rewriting")
    export.add_argument("input", help="nonempty embedded HAR 1.2 JSON or self-contained .har.zip")
    export.add_argument("--out", required=True, help="new local inventory path (never overwritten)")
    for command, help_text in (
            ("select-requests", "copy complete uniquely inventory-selected entries and referenced ZIP payloads"),
            ("plan-selection", "report selection indexes, digests and counts without writing a HAR")):
        select = sub.add_parser(command, help=help_text)
        select.add_argument("input", help="embedded HAR 1.2 JSON or self-contained .har.zip")
        select.add_argument("--inventory", required=True, help="strict target requests JSON; each must match exactly one entry")
        select.add_argument("--out", required=True, help=("new JSON report path (never overwritten)" if command == "plan-selection"
                            else "new JSON HAR path or .har.zip for ZIP input (never overwritten)"))
        if command == "select-requests":
            select.add_argument("--plan", help="prior plan-selection JSON; require exact recomputed report agreement")
    forms = sub.add_parser("embed-forms", help="explicitly serialize params-only UTF-8 URL-encoded forms into a new HAR")
    forms.add_argument("input", help="HAR 1.2 JSON with params-only forms")
    forms.add_argument("--out", required=True, help="new local HAR path (never overwritten)")
    help_text = {"plan": "write only a JSON dry-run plan",
                 "apply": "write a new HAR, failing on response conflicts or failed inventory checks",
                 "preflight": "report missing/ambiguous exact fixture matches for an explicit inventory"}
    for command in ("plan", "apply", "preflight"):
        cmd = sub.add_parser(command, help=help_text[command])
        cmd.add_argument("input", help="embedded HAR 1.2 JSON or self-contained .har.zip")
        cmd.add_argument("--rules", required=command != "preflight", help="explicit origin mappings, query keys and request-header removals JSON (preflight default: no rewrite)")
        cmd.add_argument("--inventory", required=command == "preflight", help="explicit target requests JSON; require exactly one fixture per request")
        cmd.add_argument("--out", required=True, help="new local output path (never overwritten)")
    args = parser.parse_args(argv)
    guarded_selection = args.command == "select-requests" and args.plan is not None
    try:
        if args.command in ("select-requests", "plan-selection"):
            try:
                source, wanted = Path(args.input), Path(args.inventory)
                aliases = source.resolve() == wanted.resolve() or source.samefile(wanted)
            except (OSError, ValueError, RuntimeError) as exc:
                raise DoctorError(f"cannot compare selection input paths: {exc}") from exc
            if aliases:
                raise DoctorError(f"{args.command} input and inventory must differ (including aliases)")
            if guarded_selection:
                try:
                    plan_path = Path(args.plan)
                    aliases = any(plan_path.resolve() == p.resolve() or plan_path.samefile(p)
                                  for p in (source, wanted))
                except (OSError, ValueError, RuntimeError) as exc:
                    raise DoctorError('cannot compare selection plan input paths') from exc
                if aliases:
                    raise DoctorError('selection plan must differ from HAR and inventory (including aliases)')
        fixture = load_fixture(args.input)
        har = fixture.har
        if args.command == "plan-selection":
            report = fixture.build_selection_plan(load_json(args.inventory))
            atomic_write_json(report, args.out, protected=[args.input, args.inventory])
            print(json.dumps({"command": "plan-selection", "output": args.out, **report["summary"]}, sort_keys=True))
            return 0
        if args.command == "select-requests":
            inventory = load_json(args.inventory)
            protected = [args.input, args.inventory]
            if guarded_selection:
                fixture.check_selection_plan(inventory, load_json(args.plan))
                protected.append(args.plan)
            summary = fixture.write_selection(inventory, args.out, protected=protected)
            print(json.dumps({"command": "select-requests", "output": args.out,
                              **summary,
                              "totalEntries": len(har["log"]["entries"])}, sort_keys=True))
            return 0
        if args.command == "embed-forms" and fixture.is_zip:
            raise DoctorError("embed-forms is JSON-only; archive payloads must already contain exact bytes")
        if args.command == "export-inventory":
            inventory = export_inventory(har)
            atomic_write_json(inventory, args.out, protected=[args.input])
            print(json.dumps({"command": "export-inventory", "output": args.out,
                              "requests": len(inventory["requests"])}, sort_keys=True))
            return 0
        if args.command == "embed-forms":
            result = embed_form_params(har)
            count = sum("postData" in e["request"] and "text" not in e["request"]["postData"]
                        for e in har["log"]["entries"])
            atomic_write_json(result, args.out, protected=[args.input])
            print(json.dumps({"command": "embed-forms", "output": args.out, "embeddedRequests": count}, sort_keys=True))
            return 0
        rules = load_json(args.rules) if args.rules is not None else {}
        if args.rules is not None and rules is None:
            raise DoctorError("rules: expected an object, not null")
        inventory = load_json(args.inventory) if args.inventory is not None else None
        if args.inventory is not None and inventory is None:
            raise DoctorError("inventory: expected an object, not null")
        protected = [p for p in (args.input, args.rules, args.inventory) if p is not None]
        if args.command == "preflight":
            report = build_preflight(har, inventory, rules)
            atomic_write_json(report, args.out, protected=protected)
            print(json.dumps({"command": "preflight", "output": args.out, "passed": report["passed"], **report["summary"]}, sort_keys=True))
            return 0 if report["passed"] else 2
        if args.command == "plan":
            plan = build_plan(har, rules, inventory=inventory)
            atomic_write_json(plan, args.out, protected=protected)
            print(json.dumps({"command": "plan", "output": args.out, "safeToApply": plan["safeToApply"], **plan["summary"]}, sort_keys=True))
            return 0 if plan["safeToApply"] else 2
        result, plan = rewrite_har(har, rules, inventory=inventory)
        fixture.write(result, args.out, protected=protected)
        print(json.dumps({"command": "apply", "output": args.out, **plan["summary"]}, sort_keys=True))
        return 0
    except (DoctorError, RecursionError) as exc:
        message = 'guarded selection refused; validate inputs, plan and output path' if guarded_selection else str(exc)
        print(f"qawrench har: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
