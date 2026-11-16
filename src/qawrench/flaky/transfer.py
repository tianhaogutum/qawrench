"""Deterministic source bundles, replayed through the pinned report adapters."""

import base64
import binascii
from contextlib import closing
import hashlib
import json
import sqlite3

from .adapters import (ReportError, arr, canonical, finite_float, no_duplicate_keys,
                       obj, parse_report, reject_constant, string)
from .ledger import LedgerError, _archive_column, _read_db, _write_runs

BUNDLE_FORMAT = "retry-history-ledger"
BUNDLE_VERSION = 1
RUN_FIELDS = {"run_id", "format", "project_context", "sha256", "raw_report_base64"}


def export_ledger(db_path, *, run_ids=None) -> bytes:
    """Export a read-only snapshot as canonical ASCII JSON plus one LF.

    Local import timestamps and derived rows are omitted. Exact source bytes
    and adapter contexts reconstruct the history on import. None selects all
    runs; an iterable of exact run ID strings selects only those runs (once
    each), and an empty iterable produces an empty bundle. Unknown IDs fail
    before any bundle is returned. Unselected archived/corrupt sources do not
    prevent exporting full selected runs.
    """
    selected = None
    if run_ids is not None:
        try:
            if isinstance(run_ids, (str, bytes)):
                raise ValueError("expected an iterable of run ID strings, not a scalar string")
            selected = set()
            for run_id in run_ids:
                string(run_id, "run_ids")
                run_id.encode("utf-8")
                selected.add(run_id)
        except (TypeError, ValueError) as exc:
            raise LedgerError(f"invalid run_ids: {exc}") from exc
    try:
        with closing(_read_db(db_path)) as db:
            return _export_snapshot(db, selected)
    except sqlite3.Error as exc:
        raise LedgerError(f"cannot export ledger: {exc}") from exc


def _export_snapshot(db, run_ids=None):
    """Encode selected full runs from the caller's existing snapshot/transaction."""
    runs = []
    selected = None if run_ids is None else set(run_ids)
    if selected is not None:
        known = {row[0] for row in db.execute("SELECT run_id FROM runs")}
        missing = selected - known
        if missing:
            raise LedgerError(f"unknown run-id(s): {', '.join(repr(run_id) for run_id in sorted(missing))}")
    for run_id, format, project, digest, raw, archive in db.execute(
            f"SELECT run_id,format,project_context,digest,raw_report,{_archive_column(db)} FROM runs ORDER BY run_id"):
        if selected is not None and run_id not in selected:
            continue
        if archive is not None:
            raise LedgerError(f"run-id {run_id!r}: full attempts/source archived in {json.loads(archive)['path']!r}; cannot export complete ledger")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise LedgerError(f"run-id {run_id!r}: stored source SHA-256 mismatch; cannot export ledger")
        runs.append({"run_id": run_id, "format": format, "project_context": project,
                     "sha256": digest, "raw_report_base64": base64.b64encode(raw).decode("ascii")})
    return (canonical({"ledger_format": BUNDLE_FORMAT, "version": BUNDLE_VERSION,
                       "runs": runs}) + "\n").encode("ascii")


def _parse_bundle(raw):
    try:
        root = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=no_duplicate_keys,
                          parse_float=finite_float, parse_constant=reject_constant)
        obj(root, "$")
        if set(root) != {"ledger_format", "version", "runs"}:
            raise ReportError("$: expected exactly ledger_format, version and runs")
        if (root["ledger_format"] != BUNDLE_FORMAT or type(root["version"]) is not int
                or root["version"] != BUNDLE_VERSION):
            raise ReportError("$: unsupported ledger bundle format/version (expected retry-history-ledger v1)")
        seen = {}
        for i, run in enumerate(arr(root["runs"], "$.runs")):
            path = f"$.runs[{i}]"
            obj(run, path)
            if set(run) != RUN_FIELDS:
                raise ReportError(f"{path}: expected exactly {', '.join(sorted(RUN_FIELDS))}")
            run_id = string(run["run_id"], path + ".run_id")
            run_id.encode("utf-8")  # Reject lone JSON surrogates before opening SQLite.
            format = string(run["format"], path + ".format")
            project = run["project_context"]
            if project is not None:
                string(project, path + ".project_context")
                project.encode("utf-8")
            text = string(run["raw_report_base64"], path + ".raw_report_base64")
            try:
                source = base64.b64decode(text, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ReportError(f"{path}.raw_report_base64: invalid base64") from exc
            if base64.b64encode(source).decode("ascii") != text:
                raise ReportError(f"{path}.raw_report_base64: expected canonical base64")
            digest = hashlib.sha256(source).hexdigest()
            if run["sha256"] != digest:
                raise ReportError(f"{path}.sha256: source SHA-256 mismatch")
            previous = seen.get(run_id)
            if previous is not None:
                _, previous_raw, previous_parsed = previous
                if (format, project, source) != (previous_parsed.format, previous_parsed.project_context, previous_raw):
                    raise ReportError(f"{path}: conflicting duplicate run-id {run_id!r}; nothing imported")
                continue
            parsed = parse_report(source, format, project=project)
            seen[run_id] = (run_id, source, parsed)
        return [seen[key] for key in sorted(seen)]
    except (ReportError, ValueError, RecursionError) as exc:
        raise LedgerError(f"invalid ledger bundle: {exc}") from exc


def import_ledger(db_path, raw: bytes):
    """Validate every source before opening SQLite; merge in one transaction.

    Identical run IDs/content/contexts are no-ops. Any conflicting identity or
    write failure rolls back the entire bundle, including newly created schema.
    Duplicate identical records in a bundle count once in the result.
    """
    runs = _parse_bundle(raw)
    imported, unchanged = _write_runs(db_path, runs)
    return {"action": "merged" if imported else "unchanged",
            "imported_runs": imported, "unchanged_runs": unchanged}
