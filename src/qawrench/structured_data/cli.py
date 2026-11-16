"""CLI and JSON/Markdown report output. Input scripts are never executed."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from .core import ParityError, compare_files
from .baseline import make_baseline
from qawrench import __version__


def _inline(value: object) -> str:
    # HTML-escape untrusted report data; tables must survive pipes/newlines.
    text = json.dumps(value, ensure_ascii=True, sort_keys=True)
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("|", "&#124;").replace("`", "&#96;"))


def markdown_report(report: dict) -> str:
    lines = ["# Structured data parity", "", f"Status: **{report['status']}**", ""]
    if report["status"] == "error":
        for error in report["errors"]:
            lines += [f"- **{error['code']}**: {_inline(error['message'])}",
                      f"  Source: {_inline(error['provenance'])}"]
    else:
        lines += [f"Base URL: {_inline(report['base_url'])}", "",
                  f"Changes: {report['change_count']}", "",
                  f"Policy: {_inline(report['policy'])}", "",
                  f"normalization_matches: {_inline(report['normalization_matches'])}", "",
                  f"Captures (including SHA-256): {_inline(report['captures'])}", "",
                  *([f"Baseline: {_inline(report['baseline'])}", "",
                     f"Observed: {report['observed_change_count']}; accepted: {report['accepted_change_count']}", "",
                     f"Accepted exact deltas: {_inline(report['accepted_changes'])}", ""] if 'baseline' in report else []),
                  "| Syntax / scope | Entity | JSON pointer | Kind | Old | New |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for change in report["changes"]:
            lines.append("| " + " | ".join([
                _inline(change["syntax"] + " / " + change["scope"]), _inline(change["entity"]),
                _inline(change["path"]), change["kind"],
                _inline(change["old"]) if "old" in change else "(absent)",
                _inline(change["new"]) if "new" in change else "(absent)",
            ]) + " |")
        if report["changes"]:
            lines += ["", "## Change provenance", ""]
            for index, change in enumerate(report["changes"], 1):
                lines.append(f"{index}. Old: {_inline(change.get('old_provenance'))}; new: {_inline(change.get('new_provenance'))}")
    return "\n".join(lines) + "\n"


def _write_output(path: Path, text: str) -> None:
    # Atomic replacement: a failed write leaves an existing report untouched.
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix=".parity-", delete=False) as file:
            temp = file.name
            file.write(text)
        os.replace(temp, path)
    finally:
        if temp is not None and os.path.exists(temp):
            os.unlink(temp)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qawrench structured-data", description="Compare JSON-LD/OpenGraph in two local UTF-8 HTML captures. No fetching or script execution.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("old", type=Path, help="raw / earlier HTML file")
    parser.add_argument("new", type=Path, help="rendered / later HTML file")
    parser.add_argument("--base-url", required=True, help="explicit absolute HTTP(S) capture URL used for @id pairing")
    parser.add_argument("--identity-field", default="@id", help="required top-level string key on every JSON-LD entity (default: @id)")
    parser.add_argument("--syntax", action="append", help="json-ld or opengraph; repeat to select both (default: both); RDFa unsupported")
    parser.add_argument("--set-valued", action="append", default=[], metavar="POINTER", help="entity-relative JSON pointer to an array treated as a set; repeatable")
    parser.add_argument("--normalize", action="append", nargs=2, default=[], metavar=("POINTER", "MODE"),
                        help="entity-relative string policy (array '*' tokens supported): trim, collapse-whitespace, or casefold; repeat for different pointers")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--out", type=Path, help="atomic report destination; default stdout")
    parser.add_argument("--fail-on-diff", action="store_true", help="exit 1 for valid reports with differences (default exits 0)")
    baselines = parser.add_mutually_exclusive_group()
    baselines.add_argument("--save-baseline", type=Path, metavar="JSON", help="atomically save exact accepted deltas, source hashes and coverage; reject coverage loss")
    baselines.add_argument("--baseline", type=Path, metavar="JSON", help="accept only exact saved deltas after reference hash, policy and coverage checks")
    parser.add_argument("--minimum-coverage", action="append", nargs=2, default=[], metavar=("METRIC", "COUNT"),
                        help="require a nonnegative syntax.metric count on both captures when saving a baseline; repeat for distinct metrics (e.g. json-ld.entities 1)")
    parser.add_argument("--minimum-og-property", action="append", nargs=2, default=[], metavar=("PROPERTY", "COUNT"),
                        help="require an exact OpenGraph property's value count on both captures when saving a baseline; repeat for distinct properties (e.g. og:title 1)")
    parser.add_argument("--require-jsonld-entity", action="append", default=[], metavar="ID",
                        help="require an exact resolved JSON-LD entity identity on both captures when saving a baseline; repeat for distinct identities")
    parser.add_argument("--minimum-og-nonblank-property", action="append", nargs=2, default=[], metavar=("PROPERTY", "COUNT"),
                        help="require an exact OpenGraph property's nonblank value count on both captures when saving a baseline; repeat for distinct properties")
    parser.add_argument("--require-jsonld-type", action="append", nargs=2, default=[], metavar=("ID", "TYPE"),
                        help="require an exact literal @type for a resolved JSON-LD identity on both captures when saving a baseline; repeat for distinct pairs")
    parser.add_argument("--require-jsonld-nonblank-property", action="append", nargs=2, default=[], metavar=("ID", "PROPERTY"),
                        help="require an original nonblank top-level string property for an exact JSON-LD identity on both captures when saving a baseline; repeat for distinct pairs")
    parser.add_argument("--require-jsonld-nonempty-array", action="append", nargs=2, default=[], metavar=("ID", "PROPERTY"),
                        help="require an original nonempty top-level array for an exact JSON-LD identity on both captures when saving a baseline; repeat for distinct pairs")
    parser.add_argument("--minimum-jsonld-array-nonblank-string", action="append", nargs=3, default=[], metavar=("ID", "PROPERTY", "COUNT"),
                        help="require an original top-level array with at least COUNT immediate nonblank strings for an exact JSON-LD identity when saving a baseline; repeat for distinct pairs")
    parser.add_argument("--minimum-jsonld-array-distinct-nonblank-string", action="append", nargs=3, default=[], metavar=("ID", "PROPERTY", "COUNT"),
                        help="require an original top-level array with at least COUNT distinct immediate nonblank strings for an exact JSON-LD identity when saving a baseline; repeat for distinct pairs")
    parser.add_argument("--require-jsonld-array-string", action="append", nargs=3, default=[], metavar=("ID", "PROPERTY", "MEMBER"),
                        help="require an exact original nonblank string member in a named top-level JSON-LD array on both captures when saving a baseline; repeat for conjunctive distinct triples")
    parser.add_argument("--allow-jsonld-array-string", action="append", nargs=3, default=[], metavar=("ID", "PROPERTY", "MEMBER"),
                        help="allow only these original immediate nonblank strings in an exact JSON-LD top-level array on both captures when saving a baseline; repeat distinct triples; empty arrays pass")
    parser.add_argument("--maximum-jsonld-array-nonblank-string", action="append", nargs=3, default=[], metavar=("ID", "PROPERTY", "COUNT"),
                        help="require an original top-level array with at most COUNT immediate nonblank strings, including repeats, for an exact JSON-LD identity when saving a baseline; repeat distinct pairs; zero still requires an array")
    parser.add_argument("--require-jsonld-array-all-nonblank-strings", action="append", nargs=2, default=[], metavar=("ID", "PROPERTY"),
                        help="require every original immediate element of a named top-level JSON-LD array to be a nonblank string on both captures when saving a baseline; repeat distinct pairs; empty arrays pass")
    args = parser.parse_args(argv)
    output_path = args.out
    try:
        try:
            protected = [args.old, args.new] + ([args.baseline] if args.baseline else [])
            for destination in (output_path, args.save_baseline):
                if destination is None:
                    continue
                if destination.is_symlink():
                    destination.resolve(strict=True)
                if any(destination.resolve() == p.resolve() for p in protected):
                    output_path = None
                    raise ParityError("output_conflict", "Report/baseline destinations must not overwrite HTML inputs or the loaded baseline.")
            if output_path and args.save_baseline and output_path.resolve() == args.save_baseline.resolve():
                output_path = None
                raise ParityError("output_conflict", "Report and saved baseline need separate destinations.")
        except (OSError, RuntimeError, ValueError) as exc:
            output_path = None
            raise ParityError("output_io", f"Cannot resolve report/baseline/input paths: {exc}") from exc
        normalize = {}
        for pointer, mode in args.normalize:
            if pointer in normalize:
                raise ParityError("invalid_config", f"Repeated normalization pointer {pointer!r}; choose one mode per field.")
            normalize[pointer] = mode
        minimum_coverage = {}
        if args.require_jsonld_entity and args.save_baseline is None:
            raise ParityError("invalid_config", "--require-jsonld-entity requires --save-baseline; replay uses saved requirements.")
        if args.minimum_coverage and args.save_baseline is None:
            raise ParityError("invalid_config", "--minimum-coverage requires --save-baseline; replay uses saved requirements.")
        for metric, count in args.minimum_coverage:
            if metric in minimum_coverage:
                raise ParityError("invalid_config", f"Repeated minimum coverage metric {metric!r}; choose one floor per metric.")
            try:
                minimum_coverage[metric] = int(count)
            except ValueError as exc:
                raise ParityError("invalid_config", f"Minimum coverage {metric!r} requires a nonnegative integer count.") from exc
        minimum_og_properties = {}
        if args.minimum_og_property and args.save_baseline is None:
            raise ParityError("invalid_config", "--minimum-og-property requires --save-baseline; replay uses saved requirements.")
        for name, count in args.minimum_og_property:
            if name in minimum_og_properties:
                raise ParityError("invalid_config", f"Repeated OpenGraph property {name!r}; choose one floor per property.")
            try:
                minimum_og_properties[name] = int(count)
            except ValueError as exc:
                raise ParityError("invalid_config", f"OpenGraph property {name!r} requires a nonnegative integer count.") from exc
        minimum_og_nonblank_properties = {}
        if args.minimum_og_nonblank_property and args.save_baseline is None:
            raise ParityError("invalid_config", "--minimum-og-nonblank-property requires --save-baseline; replay uses saved requirements.")
        for name, count in args.minimum_og_nonblank_property:
            if name in minimum_og_nonblank_properties:
                raise ParityError("invalid_config", f"Repeated nonblank OpenGraph property {name!r}; choose one floor per property.")
            try:
                minimum_og_nonblank_properties[name] = int(count)
            except ValueError as exc:
                raise ParityError("invalid_config", f"Nonblank OpenGraph property {name!r} requires a nonnegative integer count.") from exc
        required_jsonld_types = {}
        if args.require_jsonld_type and args.save_baseline is None:
            raise ParityError("invalid_config", "--require-jsonld-type requires --save-baseline; replay uses saved requirements.")
        for identity, required_type in args.require_jsonld_type:
            types = required_jsonld_types.setdefault(identity, [])
            if required_type in types:
                raise ParityError("invalid_config", f"Repeated required JSON-LD identity/type pair {(identity, required_type)!r}.")
            types.append(required_type)
        required_jsonld_nonblank_properties = {}
        if args.require_jsonld_nonblank_property and args.save_baseline is None:
            raise ParityError("invalid_config", "--require-jsonld-nonblank-property requires --save-baseline; replay uses saved requirements.")
        for identity, name in args.require_jsonld_nonblank_property:
            names = required_jsonld_nonblank_properties.setdefault(identity, [])
            if name in names:
                raise ParityError("invalid_config", f"Repeated required JSON-LD identity/nonblank property pair {(identity, name)!r}.")
            names.append(name)
        required_jsonld_nonempty_arrays = {}
        if args.require_jsonld_nonempty_array and args.save_baseline is None:
            raise ParityError("invalid_config", "--require-jsonld-nonempty-array requires --save-baseline; replay uses saved requirements.")
        for identity, name in args.require_jsonld_nonempty_array:
            names = required_jsonld_nonempty_arrays.setdefault(identity, [])
            if name in names:
                raise ParityError("invalid_config", f"Repeated required JSON-LD identity/nonempty array pair {(identity, name)!r}.")
            names.append(name)
        required_jsonld_array_all_nonblank_strings = {}
        if args.require_jsonld_array_all_nonblank_strings and args.save_baseline is None:
            raise ParityError("invalid_config", "--require-jsonld-array-all-nonblank-strings requires --save-baseline; replay uses saved requirements.")
        for identity, name in args.require_jsonld_array_all_nonblank_strings:
            names = required_jsonld_array_all_nonblank_strings.setdefault(identity, [])
            if name in names:
                raise ParityError("invalid_config", f"Repeated required JSON-LD all-elements identity/property pair {(identity, name)!r}.")
            names.append(name)
        minimum_jsonld_array_nonblank_strings = {}
        if args.minimum_jsonld_array_nonblank_string and args.save_baseline is None:
            raise ParityError("invalid_config", "--minimum-jsonld-array-nonblank-string requires --save-baseline; replay uses saved requirements.")
        for identity, name, count in args.minimum_jsonld_array_nonblank_string:
            properties = minimum_jsonld_array_nonblank_strings.setdefault(identity, {})
            if name in properties:
                raise ParityError("invalid_config", f"Repeated JSON-LD array nonblank string identity/property pair {(identity, name)!r}.")
            try:
                properties[name] = int(count)
            except ValueError as exc:
                raise ParityError("invalid_config", "JSON-LD array nonblank string minimums require a nonnegative integer count.") from exc
        minimum_jsonld_array_distinct_nonblank_strings = {}
        if args.minimum_jsonld_array_distinct_nonblank_string and args.save_baseline is None:
            raise ParityError("invalid_config", "--minimum-jsonld-array-distinct-nonblank-string requires --save-baseline; replay uses saved requirements.")
        for identity, name, count in args.minimum_jsonld_array_distinct_nonblank_string:
            properties = minimum_jsonld_array_distinct_nonblank_strings.setdefault(identity, {})
            if name in properties:
                raise ParityError("invalid_config", f"Repeated JSON-LD array distinct nonblank string identity/property pair {(identity, name)!r}.")
            try:
                properties[name] = int(count)
            except ValueError as exc:
                raise ParityError("invalid_config", "JSON-LD array distinct nonblank string minimums require a nonnegative integer count.") from exc
        maximum_jsonld_array_nonblank_strings = {}
        if args.maximum_jsonld_array_nonblank_string and args.save_baseline is None:
            raise ParityError("invalid_config", "--maximum-jsonld-array-nonblank-string requires --save-baseline; replay uses saved requirements.")
        for identity, name, count in args.maximum_jsonld_array_nonblank_string:
            properties = maximum_jsonld_array_nonblank_strings.setdefault(identity, {})
            if name in properties:
                raise ParityError("invalid_config", f"Repeated JSON-LD array maximum identity/property pair {(identity, name)!r}.")
            try:
                properties[name] = int(count)
            except ValueError as exc:
                raise ParityError("invalid_config", "JSON-LD array nonblank string maximums require a nonnegative integer count.") from exc
        required_jsonld_array_strings = {}
        if args.require_jsonld_array_string and args.save_baseline is None:
            raise ParityError("invalid_config", "--require-jsonld-array-string requires --save-baseline; replay uses saved requirements.")
        for identity, name, member in args.require_jsonld_array_string:
            members = required_jsonld_array_strings.setdefault(identity, {}).setdefault(name, [])
            if member in members:
                raise ParityError("invalid_config", f"Repeated required JSON-LD array string triple {(identity, name, member)!r}.")
            members.append(member)
        allowed_jsonld_array_strings = {}
        if args.allow_jsonld_array_string and args.save_baseline is None:
            raise ParityError("invalid_config", "--allow-jsonld-array-string requires --save-baseline; replay uses saved requirements.")
        for identity, name, member in args.allow_jsonld_array_string:
            members = allowed_jsonld_array_strings.setdefault(identity, {}).setdefault(name, [])
            if member in members:
                raise ParityError("invalid_config", f"Repeated allowed JSON-LD array string triple {(identity, name, member)!r}.")
            members.append(member)
        report = compare_files(args.old, args.new, base_url=args.base_url,
                               identity_field=args.identity_field,
                               syntaxes=tuple(args.syntax) if args.syntax else ("json-ld", "opengraph"),
                               set_valued=tuple(args.set_valued), normalize=normalize, baseline=args.baseline)
        if args.save_baseline:
            baseline = make_baseline(report, minimum_coverage=minimum_coverage,
                                     minimum_og_properties=minimum_og_properties,
                                     required_jsonld_entities=args.require_jsonld_entity,
                                     minimum_og_nonblank_properties=minimum_og_nonblank_properties,
                                     required_jsonld_types=required_jsonld_types,
                                     required_jsonld_nonblank_properties=required_jsonld_nonblank_properties,
                                     required_jsonld_nonempty_arrays=required_jsonld_nonempty_arrays,
                                     minimum_jsonld_array_nonblank_strings=minimum_jsonld_array_nonblank_strings,
                                     minimum_jsonld_array_distinct_nonblank_strings=minimum_jsonld_array_distinct_nonblank_strings,
                                     required_jsonld_array_strings=required_jsonld_array_strings,
                                     allowed_jsonld_array_strings=allowed_jsonld_array_strings,
                                     maximum_jsonld_array_nonblank_strings=maximum_jsonld_array_nonblank_strings,
                                     required_jsonld_array_all_nonblank_strings=required_jsonld_array_all_nonblank_strings)
            try:
                _write_output(args.save_baseline, json.dumps(baseline, ensure_ascii=True, sort_keys=True,
                                                           indent=2, allow_nan=False) + "\n")
            except OSError as exc:
                raise ParityError("baseline_io", f"Cannot write baseline: {exc}", [{"file": str(args.save_baseline)}]) from exc
    except ParityError as error:
        report = {"schema_version": 1, "tool": "structured-data-parity", "status": "error", "changes": [], "errors": [error.as_dict()]}
        print(f"{error.code}: {error}", file=sys.stderr)
    content = markdown_report(report) if args.format == "markdown" else json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n"
    try:
        if output_path:
            _write_output(output_path, content)
        else:
            sys.stdout.write(content)
    except OSError as exc:
        print(f"output_io: Cannot write report: {exc}", file=sys.stderr)
        return 2
    if report["status"] == "error":
        return 2
    return 1 if args.fail_on_diff and report["status"] == "different" else 0
