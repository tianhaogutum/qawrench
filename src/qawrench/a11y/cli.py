"""Local files in, deterministic JSON files out. No network or executable inputs."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from qawrench import __version__

from .core import BridgeError, canonical, compare, fingerprint, make_baseline, validate_baseline, validate_exceptions
from .catalog import validate_catalog
from .draft import draft_exception
from .migration import diagnose_migration
from .sarif import migration_to_sarif, to_sarif


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BridgeError(f"duplicate JSON key {key!r}; refusing data loss")
        result[key] = value
    return result


def _constant(value):
    raise BridgeError(f"nonstandard JSON number {value}")


def read_json(path):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            return json.load(stream, object_pairs_hook=_pairs, parse_constant=_constant)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeError(f"cannot read JSON {path}: {exc}") from exc


def write_json(path, value, *, overwrite=True):
    """Atomic replacement of an individual artifact; no cross-file transaction."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            json.dump(value, stream, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            # Publish the complete draft atomically, refusing even a racing creator.
            os.link(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def check_paths(inputs, outputs):
    paths = [Path(p).resolve() for p in outputs if p]
    if len(paths) != len(set(paths)):
        raise BridgeError("output paths must be different")
    inputs = {Path(p).resolve() for p in inputs}
    for path in paths:
        if path in inputs or any(path.exists() and path.samefile(p) for p in inputs if p.exists()):
            raise BridgeError("output must not overwrite an input/baseline")
        if not path.parent.is_dir() or path.is_dir():
            raise BridgeError(f"output is not a file in an existing directory: {path}")


def parser():
    root = argparse.ArgumentParser(prog="qawrench a11y", description="Offline, identity-based accessibility baselines; never scans pages.")
    root.add_argument("--version", action="version", version=__version__)
    subs = root.add_subparsers(dest="command", required=True)
    draft = subs.add_parser("draft-exception", help="draft one reviewed exception from a current delta identity")
    draft.add_argument("delta", help="local a11y-delta JSON produced by diff")
    draft.add_argument("--fingerprint", required=True, help="full lowercase SHA256 from a new or unchanged finding")
    draft.add_argument("--owner", required=True)
    draft.add_argument("--reason", required=True)
    draft.add_argument("--expires", required=True, help="explicit YYYY-MM-DD expiry (00:00 UTC)")
    draft.add_argument("--existing-reviews", help="strict review JSON to preserve while adding a new identity; never renews entries")
    draft.add_argument("--out", required=True, help="new review JSON file in an existing directory; must not exist")
    draft.add_argument("--dry-run", action="store_true", help="validate and summarize without writing files")
    for name in ("baseline", "diff", "migration"):
        sub = subs.add_parser(name)
        sub.add_argument("--format", required=True, choices=("axe", "pa11y-ci"))
        sub.add_argument("--input-version", help="pa11y-ci requires 4.0.0; axe verifies embedded 4.10.3 or 4.11.0")
        sub.add_argument("--url", help="fallback page URL for axe results without url")
        sub.add_argument("report", help="local native JSON report")
        sub.add_argument("--out", required=True, help="destination JSON file (existing directory)")
        sub.add_argument("--dry-run", action="store_true", help="validate and summarize without writing files")
        if name in ("diff", "migration"):
            sub.add_argument("--baseline", required=True)
        if name == "migration":
            sub.add_argument("--output-format", choices=("json", "sarif"), default="json",
                             help="migration output format (default: json); SARIF emits observation notes")
            sub.add_argument("--rule-map", required=True, help="explicit version-bound one-to-one rule rename map")
            sub.add_argument("--baseline-catalog", help="unfiltered axe getRules catalog for the baseline rule set")
            sub.add_argument("--current-catalog", help="unfiltered axe getRules catalog for the current rule set")
        if name == "diff":
            sub.add_argument("--exceptions", help="local reviewed-exception JSON with exact identities and bounded occurrence counts")
            sub.add_argument("--sarif", help="also write SARIF 2.1.0, including fixed findings")
            sub.add_argument("--fail-on", default="none", choices=("none", "minor", "moderate", "serious", "critical"),
                             help="fail on new occurrences at or above this severity (default: none)")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "draft-exception":
            existing_paths = [args.existing_reviews] if args.existing_reviews is not None else []
            check_paths([args.delta, *existing_paths], [args.out])
            if os.path.lexists(args.out):
                raise BridgeError("draft output already exists; choose a new file for review")
            existing = ({"existing_reviews": read_json(args.existing_reviews)} if existing_paths else {})
            output = draft_exception(read_json(args.delta), args.fingerprint,
                                     owner=args.owner, reason=args.reason, expires=args.expires, **existing)
            selected = next(entry for entry in output["exceptions"] if fingerprint(
                entry["engine"], entry["page"], entry["rule"], entry["target"]) == args.fingerprint)
            summary = {"dryRun": args.dry_run, "fingerprint": args.fingerprint,
                       "maxOccurrences": selected["maxOccurrences"]}
            if existing_paths:
                summary.update(existingEntries=len(output["exceptions"]) - 1,
                               addedEntries=1, totalEntries=len(output["exceptions"]))
            if not args.dry_run:
                write_json(args.out, output, overwrite=False)
            print(canonical(summary))
            return 0
        baseline_path = [args.baseline] if args.command != "baseline" else []
        sarif_path = getattr(args, "sarif", None)
        exception_path = getattr(args, "exceptions", None)
        migration_path = getattr(args, "rule_map", None)
        catalog_paths = [getattr(args, key, None) for key in ("baseline_catalog", "current_catalog")]
        check_paths([args.report, *baseline_path, *[p for p in (exception_path, migration_path, *catalog_paths) if p]], [args.out, sarif_path])
        current = make_baseline(read_json(args.report), args.format, url=args.url, input_version=args.input_version)
        exit_code = 0
        if args.command == "baseline":
            output = current
            summary = {"findings": len(current["findings"]), "pages": len(current["pages"])}
        elif args.command == "migration":
            output = diagnose_migration(read_json(args.baseline), current, read_json(migration_path),
                baseline_catalog=validate_catalog(read_json(args.baseline_catalog)) if args.baseline_catalog is not None else None,
                current_catalog=validate_catalog(read_json(args.current_catalog)) if args.current_catalog is not None else None)
            summary = {"counts": output["counts"]}
            if args.output_format == "sarif":
                output = migration_to_sarif(output)
        else:
            baseline = validate_baseline(read_json(args.baseline))
            exceptions = validate_exceptions(read_json(exception_path)) if exception_path else None
            output = compare(baseline, current, fail_on=args.fail_on, exceptions=exceptions)
            sarif = to_sarif(output) if sarif_path else None
            summary = {"counts": output["counts"], "gate": output["gate"]}
            if "exceptions" in output:
                summary["exceptions"] = output["exceptions"]["counts"]
            exit_code = 0 if output["gate"]["passed"] else 1
        if not args.dry_run:
            write_json(args.out, output)
            if sarif_path:
                write_json(sarif_path, sarif)
        print(canonical({"dryRun": args.dry_run, **summary}))
        return exit_code
    except (BridgeError, OSError, RecursionError, ValueError) as exc:
        print(f"qawrench a11y: {exc}", file=sys.stderr)
        return 2
