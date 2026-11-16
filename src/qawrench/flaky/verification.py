"""Read-only replay audits of explicitly selected local evidence."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .adapters import canonical, finite_float, no_duplicate_keys, number, parse_report, reject_constant, string
from .ledger import LedgerError, _archive_column, _classify, _read_db
from .transfer import _parse_bundle


def _retained_json(text):
    try:
        return json.loads(text, object_pairs_hook=no_duplicate_keys,
                          parse_float=finite_float, parse_constant=reject_constant)
    except (ValueError, TypeError, RecursionError) as exc:
        raise LedgerError(f"invalid retained JSON: {exc}") from exc


def _compare(actual, expected, location):
    """Compare the complete retained shape, distinguishing booleans from counts."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        if actual.keys() != expected.keys():
            raise LedgerError(f"{location}: field set mismatch")
        for key in sorted(expected):
            _compare(actual[key], expected[key], f"{location}.{key}")
    elif canonical(actual) != canonical(expected):
        raise LedgerError(f"{location}: replay mismatch")


def _archive_reference(archive, source_digest, label):
    ref = _retained_json(archive)
    if not isinstance(ref, dict) or set(ref) != {"path", "sha256", "source_sha256"}:
        raise LedgerError(f"{label}: invalid archive reference")
    string(ref["path"], f"{label} archive path")
    for key in ("sha256", "source_sha256"):
        if not isinstance(ref[key], str) or re.fullmatch(r"[0-9a-f]{64}", ref[key]) is None:
            raise LedgerError(f"{label}: invalid archive reference {key}")
    if ref["source_sha256"] != source_digest:
        raise LedgerError(f"{label}: stored source SHA-256 references disagree")
    return ref


def _retained_count(data, label, test_id):
    entry = _retained_json(data)
    retained = entry.get("retained_attempts") if isinstance(entry, dict) else None
    if not isinstance(retained, dict):
        raise LedgerError(f"{label} observation {test_id!r}: missing retained attempt counts")
    count = number(retained.get("available_attempts"),
                   f"{label} observation {test_id!r} available_attempts", integer=True)
    return count


def list_archives(db_path):
    """Inventory retained references/counts in one read-only snapshot.

    This discovers possible inputs to verify_archive; it does not verify any
    bundle or replay evidence. Stored paths are opaque informational strings.
    Full runs are excluded, including their source and normalized evidence.
    """
    try:
        groups = {}
        with closing(_read_db(db_path)) as db:
            for run_id, source_digest, archive in db.execute(
                    f"SELECT run_id,digest,{_archive_column(db)} FROM runs ORDER BY run_id"):
                if archive is None:
                    continue
                string(run_id, "stored run-id")
                label = f"run-id {run_id!r}"
                ref = _archive_reference(archive, source_digest, label)
                group = groups.setdefault(ref["sha256"], {
                    "archive_sha256": ref["sha256"], "paths": set(), "run_ids": [],
                    "observations": 0, "attempts": 0})
                group["paths"].add(ref["path"])
                group["run_ids"].append(run_id)
                for test_id, data in db.execute(
                        "SELECT test_id,data FROM observations WHERE run_id=? ORDER BY test_id", (run_id,)):
                    count = _retained_count(data, label, test_id)
                    group["observations"] += 1
                    group["attempts"] += count
        return {"action": "listed", "archives": [
            {**groups[digest], "paths": sorted(groups[digest]["paths"])} for digest in sorted(groups)]}
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise LedgerError(f"cannot list archives: {exc}") from exc


def list_runs(db_path, *, state="all"):
    """Discover full/archived audit targets and available counts, without replay.

    Only selected runs' references/context/counts are validated. Full-run counts
    describe local SQL rows; archived counts describe retained evidence. Neither
    proves source or bundle integrity. Source blobs and stored paths are not read.
    """
    if not isinstance(state, str) or state not in {"all", "full", "archived"}:
        raise LedgerError("state must be all, full or archived")
    try:
        runs = []
        with closing(_read_db(db_path)) as db:
            for run_id, format, project, source_digest, archive in db.execute(
                    f"SELECT run_id,format,project_context,digest,{_archive_column(db)} FROM runs ORDER BY run_id"):
                run_state = "full" if archive is None else "archived"
                if state != "all" and state != run_state:
                    continue
                string(run_id, "stored run-id")
                label = f"run-id {run_id!r}"
                if format not in ("playwright", "ctrf"):
                    raise LedgerError(f"{label}: invalid stored format")
                if format == "playwright":
                    if project is not None:
                        raise LedgerError(f"{label}: native project context must be null")
                else:
                    string(project, f"{label} CTRF project context")
                if not isinstance(source_digest, str) or re.fullmatch(r"[0-9a-f]{64}", source_digest) is None:
                    raise LedgerError(f"{label}: invalid source SHA-256")
                row = {"run_id": run_id, "format": format, "project_context": project,
                       "state": run_state, "source_sha256": source_digest}
                if archive is None:
                    observations = db.execute("SELECT count(*) FROM observations WHERE run_id=?", (run_id,)).fetchone()[0]
                    attempts = db.execute("SELECT count(*) FROM attempts WHERE run_id=?", (run_id,)).fetchone()[0]
                else:
                    ref = _archive_reference(archive, source_digest, label)
                    row["archive_sha256"] = ref["sha256"]
                    observations = attempts = 0
                    for test_id, data in db.execute(
                            "SELECT test_id,data FROM observations WHERE run_id=? ORDER BY test_id", (run_id,)):
                        attempts += _retained_count(data, label, test_id)
                        observations += 1
                runs.append({**row, "observations": observations, "attempts": attempts})
        return {"action": "listed", "runs": runs}
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise LedgerError(f"cannot list runs: {exc}") from exc


def verify_run(db_path, run_id):
    """Audit one full run's source, metadata, observations and available attempts.

    Use one read-only snapshot and the stored format/project context. Archived
    runs require verify_archive with an explicit bundle instead. No external
    paths are opened and no timestamps, schema or bytes are changed.
    """
    try:
        string(run_id, "--run-id")
        run_id.encode("utf-8")
        label = f"run-id {run_id!r}"
        with closing(_read_db(db_path)) as db:
            row = db.execute(
                f"SELECT format,project_context,digest,raw_report,metadata,{_archive_column(db)} "
                "FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise LedgerError(f"{label}: unknown run-id")
            format, project, source_digest, source, metadata, archive = row
            if archive is not None:
                raise LedgerError(f"{label}: run is archived; use verify-archive with an explicit bundle path")
            digest = hashlib.sha256(source).hexdigest()
            if digest != source_digest:
                raise LedgerError(f"{label}: source SHA-256 mismatch")
            try:
                parsed = parse_report(source, format, project=project)
            except ValueError as exc:
                raise LedgerError(f"{label}: invalid stored report: {exc}") from exc
            _compare(_retained_json(metadata), parsed.metadata, f"{label} metadata")
            stored = dict(db.execute("SELECT test_id,data FROM observations WHERE run_id=?", (run_id,)))
            if stored.keys() != {entry["test_id"] for entry in parsed.entries}:
                raise LedgerError(f"{label}: observation identity set mismatch")
            # Query all attempts for the run, including orphan rows that would
            # be missed by fetching only the expected observation IDs.
            attempts = {(test_id, index): (status, duration, data)
                        for test_id, index, status, duration, data in db.execute(
                            "SELECT test_id,attempt_index,status,duration_ms,data FROM attempts WHERE run_id=?",
                            (run_id,))}
            expected_attempts = {(entry["test_id"], attempt["index"])
                                 for entry in parsed.entries for attempt in entry["attempts"]}
            if attempts.keys() != expected_attempts:
                raise LedgerError(f"{label}: attempt identity set mismatch")
            for entry in parsed.entries:
                location = f"{label} observation {entry['test_id']!r}"
                expected = {key: value for key, value in entry.items() if key != "attempts"}
                _compare(_retained_json(stored[entry["test_id"]]), expected, location)
                for attempt in entry["attempts"]:
                    status, duration, data = attempts[entry["test_id"], attempt["index"]]
                    attempt_location = f"{location} attempt {attempt['index']}"
                    _compare(status, attempt["status"], f"{attempt_location}.status")
                    # SQLite REAL affinity converts imported integer durations
                    # to floats. Compare numeric value here, exact JSON below.
                    if type(duration) not in (int, float) or duration != attempt["duration_ms"]:
                        raise LedgerError(f"{attempt_location}.duration_ms: replay mismatch")
                    _compare(_retained_json(data), attempt, attempt_location)
        return {"action": "verified", "run_id": run_id, "source_sha256": digest,
                "observations": len(parsed.entries), "attempts": len(attempts)}
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise LedgerError(f"cannot verify run: {exc}") from exc


def verify_archive(db_path, archive_path):
    """Verify an explicit v1 bundle against one read-only ledger snapshot.

    Byte-identical relocated bundles work: stored paths are informational and
    are never opened. Every bundle run must be archived in this database, and
    every database reference to this bundle digest must be present. Replay
    compares all retained metadata/observation fields, not only aggregate totals.
    No database, archive, schema or stored path is changed.
    """
    try:
        raw = Path(archive_path).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        replay = _parse_bundle(raw)
        if not replay:
            raise LedgerError("empty archive: expected at least one archived run")
        with closing(_read_db(db_path)) as db:
            rows = {row[0]: row[1:] for row in db.execute(
                f"SELECT run_id,format,project_context,digest,raw_report,metadata,{_archive_column(db)} FROM runs")}
            references = {}
            for run_id, row in rows.items():
                if row[-1] is None:
                    continue
                ref = _retained_json(row[-1])
                if (not isinstance(ref, dict) or set(ref) != {"path", "sha256", "source_sha256"}
                        or not all(isinstance(value, str) for value in ref.values())):
                    raise LedgerError(f"run-id {run_id!r}: invalid archive reference")
                references[run_id] = ref
            observations = attempts = 0
            for run_id, source, parsed in replay:
                label = f"run-id {run_id!r}"
                if run_id not in rows:
                    raise LedgerError(f"{label}: unknown run-id in archive")
                if run_id not in references:
                    raise LedgerError(f"{label}: run is not archived")
                format, project, source_digest, source_blob, metadata, _ = rows[run_id]
                ref = references[run_id]
                if ref["sha256"] != digest:
                    raise LedgerError(f"{label}: archive SHA-256 mismatch")
                actual_source_digest = hashlib.sha256(source).hexdigest()
                if source_digest != actual_source_digest or ref["source_sha256"] != actual_source_digest:
                    raise LedgerError(f"{label}: stored source SHA-256 mismatch")
                if (format, project) != (parsed.format, parsed.project_context):
                    raise LedgerError(f"{label}: format/project context mismatch")
                _compare(_retained_json(metadata), parsed.metadata, f"{label} metadata")
                if source_blob != b"":
                    raise LedgerError(f"{label}: archived source blob must be empty")
                if db.execute("SELECT count(*) FROM attempts WHERE run_id=?", (run_id,)).fetchone()[0]:
                    raise LedgerError(f"{label}: archived attempt rows must be empty")
                stored = dict(db.execute("SELECT test_id,data FROM observations WHERE run_id=?", (run_id,)))
                expected_ids = {entry["test_id"] for entry in parsed.entries}
                if stored.keys() != expected_ids:
                    raise LedgerError(f"{label}: observation identity set mismatch")
                for entry in parsed.entries:
                    available = entry["attempts"]
                    expected = {key: value for key, value in entry.items() if key not in {"source", "attempts"}}
                    expected["retained_attempts"] = {
                        "classification": _classify(entry, available),
                        "available_attempts": len(available),
                        "available_attempt_duration_ms": sum(attempt["duration_ms"] for attempt in available)}
                    _compare(_retained_json(stored[entry["test_id"]]), expected,
                             f"{label} observation {entry['test_id']!r}")
                    observations += 1
                    attempts += len(available)
            missing = {run_id for run_id, ref in references.items() if ref["sha256"] == digest} - {run_id for run_id, _, _ in replay}
            if missing:
                raise LedgerError(f"missing referenced run-id(s) in archive: {', '.join(repr(key) for key in sorted(missing))}")
        return {"action": "verified", "archive_sha256": digest,
                "run_ids": [run_id for run_id, _, _ in replay], "observations": observations, "attempts": attempts}
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise LedgerError(f"cannot verify archive: {exc}") from exc
