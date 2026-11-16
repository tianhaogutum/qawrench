"""Versioned SQLite storage. Imports and schema creation share one transaction."""

from collections import Counter
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from .adapters import (CTRF_STATUSES, PW_STATUSES, boolean, canonical, choice, finite_float,
                       no_duplicate_keys, number, obj, parse_report, reject_constant, string)

APPLICATION_ID = 0x52484C31  # RHL1
SCHEMA_VERSION = 1
RETENTION_SCHEMA_VERSION = 2
DDL = (
    "CREATE TABLE runs (run_id TEXT PRIMARY KEY, format TEXT NOT NULL, digest TEXT NOT NULL, "
    "project_context TEXT, imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
    "raw_report BLOB NOT NULL, metadata TEXT NOT NULL)",
    "CREATE TABLE observations (run_id TEXT NOT NULL REFERENCES runs(run_id), test_id TEXT NOT NULL, "
    "data TEXT NOT NULL, PRIMARY KEY(run_id, test_id))",
    "CREATE TABLE attempts (run_id TEXT NOT NULL, test_id TEXT NOT NULL, attempt_index INTEGER NOT NULL, "
    "status TEXT NOT NULL, duration_ms REAL NOT NULL CHECK(duration_ms >= 0), data TEXT NOT NULL, "
    "PRIMARY KEY(run_id, test_id, attempt_index), "
    "FOREIGN KEY(run_id, test_id) REFERENCES observations(run_id, test_id))",
)


class LedgerError(ValueError):
    """Database is incompatible, inaccessible or conflicts with an existing run."""


def _check_schema(db, *, initialize=False):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    app_id = db.execute("PRAGMA application_id").fetchone()[0]
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not tables and version == app_id == 0 and initialize:
        for statement in DDL:
            db.execute(statement)
        db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    elif version not in {SCHEMA_VERSION, RETENTION_SCHEMA_VERSION} or app_id != APPLICATION_ID or tables != {"runs", "observations", "attempts"}:
        raise LedgerError("not a retry-history-ledger v1/v2 database; no automatic migration or overwrite")


def _archive_column(db):
    """The explicit prune operation adds this column when upgrading v1 to v2."""
    return "archive" if db.execute("PRAGMA user_version").fetchone()[0] == RETENTION_SCHEMA_VERSION else "NULL"


def import_report(db_path, raw: bytes, *, format: str, run_id: str, project=None, dry_run=False):
    """Validate first; byte-identical input+format+project is an idempotent no-op.

    Dry run validates input only, without opening or querying the database.
    """
    string(run_id, "--run-id")
    parsed = parse_report(raw, format, project=project)
    result = {"run_id": run_id, "format": format, "test_count": len(parsed.entries),
              "attempt_count": sum(len(e["attempts"]) for e in parsed.entries),
              "attempt_details_unavailable": sum(e["attempt_details_unavailable"] for e in parsed.entries)}
    if dry_run:
        return {**result, "action": "validated", "database_checked": False}
    imported, _ = _write_runs(db_path, [(run_id, raw, parsed)])
    return {**result, "action": "imported" if imported else "unchanged"}


def _write_runs(db_path, runs):
    """Store fully validated (run_id, source bytes, ParsedReport) tuples atomically."""
    imported = unchanged = 0
    try:
        with closing(sqlite3.connect(db_path, timeout=5)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                db.execute("BEGIN IMMEDIATE")
                _check_schema(db, initialize=True)
                for run_id, raw, parsed in runs:
                    digest = hashlib.sha256(raw).hexdigest()
                    existing = db.execute("SELECT format, digest, project_context FROM runs WHERE run_id=?", (run_id,)).fetchone()
                    if existing is not None:
                        if existing != (parsed.format, digest, parsed.project_context):
                            raise LedgerError(f"run-id {run_id!r} already exists with different content, format or project; nothing imported")
                        unchanged += 1
                        continue
                    db.execute("INSERT INTO runs(run_id,format,digest,project_context,raw_report,metadata) VALUES(?,?,?,?,?,?)",
                               (run_id, parsed.format, digest, parsed.project_context, raw, canonical(parsed.metadata)))
                    for entry in parsed.entries:
                        data = {k: v for k, v in entry.items() if k != "attempts"}
                        db.execute("INSERT INTO observations VALUES(?,?,?)", (run_id, entry["test_id"], canonical(data)))
                        for attempt in entry["attempts"]:
                            db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?)",
                                       (run_id, entry["test_id"], attempt["index"], attempt["status"],
                                        attempt["duration_ms"], canonical(attempt)))
                    imported += 1
        return imported, unchanged
    except (sqlite3.Error, OSError) as exc:
        raise LedgerError(f"SQLite import failed (transaction rolled back): {exc}") from exc


def _read_db(db_path):
    try:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            db.execute("BEGIN")
            _check_schema(db)
        except Exception:
            db.close()
            raise
        return db
    except (sqlite3.Error, OSError) as exc:
        raise LedgerError(f"cannot read ledger: {exc}") from exc


def raw_report(db_path, run_id: str) -> bytes:
    """Recover the exact imported bytes, including unknown ancillary fields."""
    try:
        with closing(_read_db(db_path)) as db:
            row = db.execute(f"SELECT raw_report,{_archive_column(db)} FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise LedgerError(f"unknown run-id {run_id!r}")
            if row[1] is not None:
                raise LedgerError(f"run-id {run_id!r}: full attempts/source archived in {json.loads(row[1])['path']!r}; import that bundle into a separate ledger to recover")
            return row[0]
    except sqlite3.Error as exc:
        raise LedgerError(f"cannot export report: {exc}") from exc


def _classify(entry, attempts):
    last = attempts[-1]["status"] if attempts else "not_run"
    native = entry["expected_status"] is not None
    expected_failure = native and last == entry["expected_status"] and last in {"failed", "timedOut"}
    # CTRF explicitly maps an expected failure to reported 'passed'. Preserve
    # the actual status, while avoiding an unexpected-failure classification.
    if not native and entry["reported_status"] == "passed" and last in {"failed", "timedOut"}:
        expected_failure = True
    unexpected_pass = native and last == "passed" and entry["expected_status"] != "passed"
    failure_then_pass = (last == "passed" and not unexpected_pass
                         and any(a["status"] in {"failed", "timedOut"} for a in attempts[:-1]))
    # Legacy CTRF maps interrupted to failed, so this weaker evidence cannot
    # establish a genuine failure followed by a pass. Keep a separate metric.
    fail_then_pass = failure_then_pass and native
    mapped_fail_then_pass = failure_then_pass and not native
    return {"terminal_status": last, "fail_then_pass": fail_then_pass,
            "mapped_fail_then_pass": mapped_fail_then_pass,
            "terminal_failure": last in {"failed", "timedOut"} and not expected_failure,
            "expected_failure": expected_failure, "unexpected_pass": unexpected_pass,
            "interrupted": last == "interrupted", "skipped": last == "skipped",
            "passed": last == "passed" and not unexpected_pass,
            "not_run": last == "not_run"}


def _report_observation(data, location):
    try:
        return obj(json.loads(data, object_pairs_hook=no_duplicate_keys,
                              parse_float=finite_float, parse_constant=reject_constant), location)
    except (ValueError, TypeError, RecursionError) as exc:
        raise LedgerError(f"{location}: invalid observation JSON or excessive nesting: {exc}") from exc


def _duration_sum(value, location):
    # Native reported durations and retained available durations are sums, so
    # the per-attempt safe-integer ceiling does not apply to them.
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise LedgerError(f"{location}: expected finite nonnegative number")
    return value


def _report_attempt(data, location, *, format):
    path = f"{location}.duration_ms"
    try:
        attempt = obj(json.loads(data, object_pairs_hook=no_duplicate_keys,
                                 parse_float=finite_float, parse_constant=reject_constant), location)
    except (ValueError, TypeError, RecursionError) as exc:
        raise LedgerError(f"{path}: cannot read duration from invalid attempt JSON or excessive nesting: {exc}") from exc
    number(attempt.get("duration_ms"), path)
    statuses = PW_STATUSES if format == "playwright" else PW_STATUSES | CTRF_STATUSES
    choice(attempt.get("status"), statuses, f"{location}.status")
    return attempt


def _report_duration(entry, *, format, location):
    path = f"{location}.reported_duration_ms"
    _duration_sum(entry.get("reported_duration_ms"), path)
    if format == "ctrf":
        # CTRF reports one final-test duration, rather than a native attempt sum.
        number(entry["reported_duration_ms"], path)


def _report_retained(entry, attempts, *, archived, location):
    """Check summary shape/state, without replaying or authenticating evidence."""
    if not archived:
        if "retained_attempts" in entry:
            raise LedgerError(f"{location}: full observation cannot have retained_attempts")
        return None
    if attempts:
        raise LedgerError(f"{location}: archived observation cannot have local attempts")
    path = f"{location}.retained_attempts"
    retained = obj(entry.get("retained_attempts"), path)
    if retained.keys() != {"classification", "available_attempts", "available_attempt_duration_ms"}:
        raise LedgerError(f"{path}: expected classification, available_attempts and available_attempt_duration_ms only")
    number(retained["available_attempts"], f"{path}.available_attempts", integer=True)
    # This is a sum of individual durations, so it can legitimately exceed
    # the adapters' per-attempt 2^53-1 limit. Do not cap the retained sum.
    duration = retained["available_attempt_duration_ms"]
    try:
        valid_duration = type(duration) in (int, float) and math.isfinite(duration) and duration >= 0
    except OverflowError:
        valid_duration = False
    if not valid_duration:
        raise LedgerError(f"{path}.available_attempt_duration_ms: expected finite nonnegative number")
    flags = obj(retained["classification"], f"{path}.classification")
    booleans = {"fail_then_pass", "mapped_fail_then_pass", "terminal_failure", "expected_failure",
                "unexpected_pass", "interrupted", "skipped", "passed", "not_run"}
    if flags.keys() != booleans | {"terminal_status"}:
        raise LedgerError(f"{path}.classification: field set mismatch")
    choice(flags["terminal_status"], PW_STATUSES | CTRF_STATUSES | {"not_run"}, f"{path}.classification.terminal_status")
    for key in sorted(booleans):
        boolean(flags[key], f"{path}.classification.{key}")
    return retained


def report(db_path, *, run_ids=None):
    """Summarize exact selected runs in one read-only snapshot.

    None selects all runs; an iterable of literal IDs selects each once, and an
    empty iterable returns an empty report. Membership is checked before any
    evidence is decoded. Unselected evidence is outside this summary, which
    neither replays sources nor opens stored archive/attachment paths.
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
            known = [row[0] for row in db.execute("SELECT run_id FROM runs ORDER BY run_id")]
            if selected is not None:
                missing = selected - set(known)
                if missing:
                    raise LedgerError(f"unknown run-id(s): {', '.join(repr(key) for key in sorted(missing))}")
            runs = []
            histories = {}
            totals = Counter()
            archive_column = _archive_column(db)
            for run_id in known:
                if selected is not None and run_id not in selected:
                    continue
                format, metadata, archive = db.execute(
                    f"SELECT format,metadata,{archive_column} FROM runs WHERE run_id=?", (run_id,)).fetchone()
                meta = json.loads(metadata)
                observations = []
                run_totals = Counter()
                for test_id, data in db.execute("SELECT test_id,data FROM observations WHERE run_id=? ORDER BY test_id", (run_id,)):
                    location = f"run-id {run_id!r} observation {test_id!r}"
                    entry = _report_observation(data, location)
                    number(entry.get("retries"), f"{location}.retries", integer=True)
                    for key in ("reported_flaky", "attempt_details_unavailable", "status_detail_unavailable"):
                        boolean(entry.get(key), f"{location}.{key}")
                    _report_duration(entry, format=format, location=location)
                    attempts = [_report_attempt(text, f"{location} attempt {index!r}", format=format) for index, text in db.execute(
                        "SELECT attempt_index,data FROM attempts WHERE run_id=? AND test_id=? ORDER BY attempt_index", (run_id, test_id))]
                    retained = _report_retained(entry, attempts, archived=archive is not None, location=location)
                    flags = retained["classification"] if retained else _classify(entry, attempts)
                    attempt_count = retained["available_attempts"] if retained else len(attempts)
                    for key, value in flags.items():
                        if isinstance(value, bool):
                            run_totals[key] += int(value)
                    run_totals["observations"] += 1
                    run_totals["available_attempts"] += attempt_count
                    run_totals["retried_tests"] += entry["retries"] > 0
                    run_totals["reported_retries"] += entry["retries"]
                    run_totals["attempt_details_unavailable"] += entry["attempt_details_unavailable"]
                    run_totals["status_detail_unavailable"] += entry["status_detail_unavailable"]
                    run_totals["reported_flaky"] += entry["reported_flaky"]
                    duration = retained["available_attempt_duration_ms"] if retained else sum(a["duration_ms"] for a in attempts)
                    run_totals["available_attempt_duration_ms"] += duration
                    run_totals["reported_test_duration_ms"] += entry["reported_duration_ms"]
                    observation = {k: v for k, v in entry.items() if k not in {"source", "retained_attempts"}}
                    observation.update(flags)
                    observation["attempts"] = [{k: v for k, v in a.items() if k != "source"} for a in attempts]
                    if retained:
                        observation["attempts_archived"] = True
                        observation["available_attempt_count"] = attempt_count
                    observation["available_attempt_duration_ms"] = duration
                    observations.append(observation)
                    history = histories.setdefault(test_id, {"test_id": test_id, "format": format,
                        "project": entry["project"], "project_id": entry["project_id"],
                        "file": entry["file"], "title_path": entry["title_path"], "run_ids": [],
                        "fail_then_pass_runs": [], "reported_flaky_runs": [], "mapped_fail_then_pass_runs": [], "terminal_failure_runs": [],
                        "retried_runs": [], "attempt_details_unavailable_runs": []})
                    history["run_ids"].append(run_id)
                    for field, flag in (("fail_then_pass_runs", flags["fail_then_pass"]),
                                        ("reported_flaky_runs", entry["reported_flaky"]),
                                        ("mapped_fail_then_pass_runs", flags["mapped_fail_then_pass"]),
                                        ("terminal_failure_runs", flags["terminal_failure"]),
                                        ("retried_runs", entry["retries"] > 0),
                                        ("attempt_details_unavailable_runs", entry["attempt_details_unavailable"])):
                        if flag:
                            history[field].append(run_id)
                run_totals["wall_duration_ms"] = meta["wall_duration_ms"]
                run_totals["global_error_count"] = len(meta["global_errors"])
                totals.update(run_totals)
                run = {"run_id": run_id, "format": format, "metadata": meta,
                       "totals": _totals(run_totals), "observations": observations}
                if archive is not None:
                    run["archive"] = json.loads(archive)
                runs.append(run)
            all_tests = []
            for history in sorted(histories.values(), key=lambda h: h["test_id"]):
                history["run_count"] = len(history["run_ids"])
                history["recurring_fail_then_pass"] = len(history["fail_then_pass_runs"]) >= 2
                history["recurring_reported_flaky"] = len(history["reported_flaky_runs"]) >= 2
                all_tests.append(history)
            return {"schema_version": db.execute("PRAGMA user_version").fetchone()[0], "run_count": len(runs), "unique_tests": len(all_tests),
                    "totals": _totals(totals), "tests": all_tests, "runs": runs,
                    "notes": ["Durations are milliseconds; available attempt sums exclude missing attempts",
                              "Retry incidence = retried test observations / all test observations",
                              "Adapters have separate identity namespaces; no cross-format or cross-rename guessing",
                              "CTRF mapped failures cannot distinguish prior interruptions/timeouts",
                              "Reported flaky and observed fail-then-pass are separate evidence"]}
    except (sqlite3.Error, ValueError, KeyError, TypeError, RecursionError) as exc:
        raise LedgerError(f"cannot summarize ledger: {exc}") from exc


def _totals(counter):
    keys = ("observations", "available_attempts", "retried_tests", "reported_retries", "fail_then_pass",
            "mapped_fail_then_pass", "terminal_failure", "expected_failure", "unexpected_pass", "interrupted", "skipped", "passed",
            "not_run", "attempt_details_unavailable", "status_detail_unavailable", "reported_flaky",
            "available_attempt_duration_ms", "reported_test_duration_ms", "wall_duration_ms", "global_error_count")
    result = {k: counter[k] for k in keys}
    result["retry_incidence"] = counter["retried_tests"] / counter["observations"] if counter["observations"] else 0.0
    result["attempt_duration_complete"] = counter["attempt_details_unavailable"] == 0
    return result


def markdown(data):
    """Compact Markdown; full attempt evidence is available in JSON/SQLite."""
    def safe(value):
        return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;").replace("\n", " ").replace("\r", " ").replace("`", "&#96;")

    t = data["totals"]
    lines = ["# Retry history ledger", "", f"Runs: {data['run_count']}; unique tests: {data['unique_tests']}", "",
             f"Retried test observations: {t['retried_tests']}/{t['observations']} ({t['retry_incidence']:.1%})",
             f"Observed fail-then-pass: {t['fail_then_pass']}; terminal failures: {t['terminal_failure']}",
             f"Passed: {t['passed']}; skipped: {t['skipped']}; interrupted: {t['interrupted']}; not run: {t['not_run']}",
             f"Expected failures: {t['expected_failure']}; unexpected passes: {t['unexpected_pass']}",
             f"Available attempt duration: {t['available_attempt_duration_ms']:g} ms; run wall duration sum: {t['wall_duration_ms']:g} ms",
             f"Unavailable attempt details: {t['attempt_details_unavailable']}; reduced status detail: {t['status_detail_unavailable']}",
             f"Reported flaky observations: {t['reported_flaky']}; mapped CTRF fail-then-pass: {t['mapped_fail_then_pass']}; global errors: {t['global_error_count']}", "",
             "| Run | Format | Tests | Retried | Fail-then-pass | Terminal failure | Missing attempt details |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for run in data["runs"]:
        r = run["totals"]
        lines.append(f"| {safe(run['run_id'])} | {run['format']} | {r['observations']} | {r['retried_tests']} | {r['fail_then_pass']} | {r['terminal_failure']} | {r['attempt_details_unavailable']} |")
    archived = sum("archive" in run for run in data["runs"])
    if archived:
        lines += ["", f"Full attempts/source archived for {archived} runs; totals retain historical evidence. See JSON for archive paths and SHA-256 digests."]
    lines += ["", "## Recurring observed fail-then-pass", ""]
    recurring = [test for test in data["tests"] if test["recurring_fail_then_pass"]]
    for test in recurring:
        lines.append(f"- {safe(test['project'])} / {safe(test['file'])} / {safe(' / '.join(test['title_path']))}: {len(test['fail_then_pass_runs'])} runs")
    if not recurring:
        lines.append("None observed in at least two runs.")
    lines += ["", *[f"- {note}" for note in data["notes"]], ""]
    return "\n".join(lines)
