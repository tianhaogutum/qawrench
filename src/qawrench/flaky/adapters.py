"""Strict adapters for the explicitly documented upstream dialects.

Unknown ancillary fields are retained in the original report by the ledger;
unknown statuses, versions and ambiguous identities are never silently dropped.
"""

from dataclasses import dataclass
import hashlib
import json
import math


class ReportError(ValueError):
    """Input is not a complete report in a supported dialect."""


PW_STATUSES = {"passed", "failed", "timedOut", "skipped", "interrupted"}
CTRF_STATUSES = {"passed", "failed", "skipped", "pending", "other"}
PW_OUTCOMES = {"expected", "unexpected", "flaky", "skipped"}


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def obj(value, path):
    if not isinstance(value, dict):
        raise ReportError(f"{path}: expected object")
    return value


def arr(value, path):
    if not isinstance(value, list):
        raise ReportError(f"{path}: expected array")
    return value


def string(value, path, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ReportError(f"{path}: expected {'possibly empty ' if empty else 'nonempty '}string")
    return value


def number(value, path, *, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or (integer and not isinstance(value, int))):
        raise ReportError(f"{path}: expected nonnegative {'integer' if integer else 'finite number'}")
    try:
        valid = math.isfinite(value) and 0 <= value <= 2**53 - 1
    except OverflowError:
        valid = False
    if not valid:
        raise ReportError(f"{path}: number must be finite, nonnegative and <= 2^53-1")
    return value


def choice(value, allowed, path):
    if not isinstance(value, str) or value not in allowed:
        raise ReportError(f"{path}: unsupported status {value!r}; expected {', '.join(sorted(allowed))}")
    return value


def boolean(value, path):
    if not isinstance(value, bool):
        raise ReportError(f"{path}: expected boolean")
    return value


def finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ReportError(f"JSON: nonfinite number {text}")
    return value


def reject_constant(value):
    raise ReportError(f"JSON: invalid number {value}")


def no_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ReportError(f"JSON: duplicate object key {key!r}")
        value[key] = item
    return value


@dataclass(frozen=True)
class ParsedReport:
    format: str
    project_context: str | None
    entries: list[dict]
    metadata: dict


def parse_report(raw: bytes, format: str, *, project: str | None = None) -> ParsedReport:
    if format not in {"playwright", "ctrf"}:
        raise ReportError(f"unsupported format {format!r}")
    try:
        root = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=no_duplicate_keys, parse_float=finite_float,
                          parse_constant=reject_constant)
        obj(root, "$")
        if format == "playwright":
            if project is not None:
                raise ReportError("--project applies only to CTRF; native reports already identify projects")
            entries, metadata = _playwright(root)
        else:
            string(project, "--project (required for CTRF; upstream has no project field)")
            entries, metadata = _ctrf(root, project)
        seen = set()
        for entry in entries:
            # Namespace adapters: legacy CTRF has an opaque, lossy title hierarchy.
            identity = [format, entry["project"], entry["project_id"], entry["file"], entry["title_path"]]
            key = hashlib.sha256(canonical(identity).encode()).hexdigest()
            if key in seen:
                raise ReportError(f"duplicate test identity: {canonical(identity)}; repeatEach/merged duplicates unsupported")
            seen.add(key)
            entry["test_id"] = key
        metadata["test_count"] = len(entries)
        return ParsedReport(format, project, entries, metadata)
    except ReportError:
        raise
    except (ValueError, RecursionError) as exc:
        raise ReportError(f"invalid UTF-8 JSON or excessive nesting: {exc}") from exc


def _playwright(root):
    config = obj(root.get("config"), "$.config")
    if config.get("version") != "1.56.1":
        raise ReportError("$.config.version: only Playwright 1.56.1 JSON dialect is supported")
    projects = {}
    for i, p in enumerate(arr(config.get("projects"), "$.config.projects")):
        path = f"$.config.projects[{i}]"
        obj(p, path)
        pid = string(p.get("id"), path + ".id", empty=True)
        name = string(p.get("name"), path + ".name", empty=True)
        if pid in projects:
            raise ReportError(path + ": duplicate project id")
        projects[pid] = name
    stats = obj(root.get("stats"), "$.stats")
    wall = number(stats.get("duration"), "$.stats.duration")
    string(stats.get("startTime"), "$.stats.startTime")
    counts = {s: number(stats.get(s), f"$.stats.{s}", integer=True) for s in PW_OUTCOMES}
    errors = arr(root.get("errors"), "$.errors")
    entries = []

    def visit(suites, ancestors, path):
        for si, suite in enumerate(arr(suites, path)):
            sp = f"{path}[{si}]"
            obj(suite, sp)
            title = string(suite.get("title"), sp + ".title", empty=True)
            string(suite.get("file"), sp + ".file")
            # Top-level suite title is the file name; the file is already a key.
            titles = ancestors + [title]
            for i, spec in enumerate(arr(suite.get("specs"), sp + ".specs")):
                pp = f"{sp}.specs[{i}]"
                obj(spec, pp)
                file = string(spec.get("file"), pp + ".file")
                name = string(spec.get("title"), pp + ".title")
                tests = arr(spec.get("tests"), pp + ".tests")
                if not tests:
                    raise ReportError(pp + ".tests: empty (partial report)")
                for ti, test in enumerate(tests):
                    tp = f"{pp}.tests[{ti}]"
                    obj(test, tp)
                    project = string(test.get("projectName"), tp + ".projectName", empty=True)
                    pid = string(test.get("projectId"), tp + ".projectId", empty=True)
                    if pid not in projects or projects[pid] != project:
                        raise ReportError(tp + ": projectId/projectName do not match config.projects")
                    expected = choice(test.get("expectedStatus"), PW_STATUSES, tp + ".expectedStatus")
                    outcome = choice(test.get("status"), PW_OUTCOMES, tp + ".status")
                    attempts = []
                    for ai, result in enumerate(arr(test.get("results"), tp + ".results")):
                        rp = f"{tp}.results[{ai}]"
                        obj(result, rp)
                        retry = number(result.get("retry"), rp + ".retry", integer=True)
                        if retry != ai:
                            raise ReportError(rp + ".retry: expected contiguous zero-based attempts (partial report)")
                        status = choice(result.get("status"), PW_STATUSES, rp + ".status")
                        duration = number(result.get("duration"), rp + ".duration")
                        attempts.append({"index": ai, "status": status, "duration_ms": duration,
                                         "status_source": "native", "source": result})
                    if not attempts and outcome != "skipped":
                        raise ReportError(tp + ".results: empty for a non-skipped outcome (partial report)")
                    entries.append({"project": project, "project_id": pid, "file": file,
                                    "title_path": titles[1:] + [name], "expected_status": expected,
                                    "reported_status": outcome, "reported_flaky": outcome == "flaky",
                                    "retries": max(0, len(attempts) - 1), "attempts": attempts,
                                    "attempt_details_unavailable": not attempts,
                                    "status_detail_unavailable": False, "identity_path_opaque": False,
                                    "reported_duration_ms": sum(a["duration_ms"] for a in attempts),
                                    "source": test})
            visit(suite.get("suites", []), titles, sp + ".suites")

    visit(root.get("suites"), [], "$.suites")
    actual = {s: sum(e["reported_status"] == s for e in entries) for s in PW_OUTCOMES}
    if actual != counts:
        raise ReportError(f"$.stats: outcome counts do not match tests (partial report): declared {counts}, actual {actual}")
    return entries, {"dialect": "playwright-1.56.1", "wall_duration_ms": wall,
                     "global_errors": errors, "run_completion_unavailable": True}


def _ctrf(root, project):
    if "reportFormat" in root and root["reportFormat"] != "CTRF":
        raise ReportError("$.reportFormat: expected CTRF")
    if "specVersion" in root and root["specVersion"] != "0.0.0":
        raise ReportError("$.specVersion: only the reporter 0.0.29 / 0.0.0 dialect is supported")
    results = obj(root.get("results"), "$.results")
    tool = obj(results.get("tool"), "$.results.tool")
    if tool.get("name") != "playwright":
        raise ReportError("$.results.tool.name: only playwright CTRF is supported")
    summary = obj(results.get("summary"), "$.results.summary")
    count = number(summary.get("tests"), "$.results.summary.tests", integer=True)
    counts = {s: number(summary.get(s), f"$.results.summary.{s}", integer=True) for s in CTRF_STATUSES}
    start = number(summary.get("start"), "$.results.summary.start")
    stop = number(summary.get("stop"), "$.results.summary.stop")
    if stop < start:
        raise ReportError("$.results.summary.stop: precedes start (partial report)")
    entries = []
    raw_counts = {s: 0 for s in CTRF_STATUSES}
    for i, test in enumerate(arr(results.get("tests"), "$.results.tests")):
        p = f"$.results.tests[{i}]"
        obj(test, p)
        name = string(test.get("name"), p + ".name")
        file = string(test.get("filePath"), p + ".filePath (comprehensive report required)")
        suite = string(test.get("suite"), p + ".suite", empty=True)
        status = choice(test.get("status"), CTRF_STATUSES, p + ".status")
        duration = number(test.get("duration"), p + ".duration")
        retries = number(test.get("retries"), p + ".retries", integer=True)
        flaky = boolean(test.get("flaky"), p + ".flaky")
        raw_status = test.get("rawStatus")
        if raw_status is not None:
            choice(raw_status, PW_STATUSES | {"pending"}, p + ".rawStatus")
        mapped = {"timedOut": "failed", "interrupted": "failed"}.get(raw_status, raw_status)
        # Reporter 0.0.29 summary counts raw statuses, while expected failures
        # can be emitted as test.status='passed'. Account for that real behavior.
        if raw_status is not None and status != mapped and not (status == "passed" and raw_status != "skipped"):
            raise ReportError(p + ": status contradicts rawStatus")
        raw_counts[mapped if mapped is not None else status] += 1
        prior = arr(test.get("retryAttempts", []), p + ".retryAttempts")
        if "retryAttempts" in test and len(prior) != retries:
            raise ReportError(p + ".retryAttempts: length differs from retries (partial report)")
        attempts = []
        for ai, result in enumerate(prior):
            rp = f"{p}.retryAttempts[{ai}]"
            obj(result, rp)
            attempts.append({"index": ai, "status": choice(result.get("status"), CTRF_STATUSES, rp + ".status"),
                             "duration_ms": number(result.get("duration"), rp + ".duration"),
                             "status_source": "ctrf-mapped", "source": result})
        # The final observation really exists. Missing earlier attempts are NOT
        # filled; its index comes only from the reported retry counter.
        attempts.append({"index": retries, "status": raw_status or status, "duration_ms": duration,
                         "status_source": "rawStatus" if raw_status else "ctrf-mapped", "source": test})
        entries.append({"project": project, "project_id": None, "file": file,
                        "title_path": [suite, name], "expected_status": None,
                        "reported_status": status, "reported_flaky": flaky, "retries": retries,
                        "attempts": attempts, "attempt_details_unavailable": retries > len(prior),
                        "status_detail_unavailable": bool(prior) or raw_status is None,
                        "identity_path_opaque": True, "reported_duration_ms": duration, "source": test})
    if count != len(entries) or sum(counts.values()) != count or counts != raw_counts:
        raise ReportError(f"$.results.summary: counts do not match tests/rawStatus (partial report): declared {counts}, actual {raw_counts}")
    return entries, {"dialect": "playwright-ctrf-json-reporter-0.0.29", "wall_duration_ms": stop - start,
                     "global_errors": [], "run_completion_unavailable": True,
                     "warnings": ["CTRF may omit unexecuted tests; total discovered test count is unavailable",
                                  "Legacy suite strings are opaque; project supplied by importer",
                                  "Mapped prior failed statuses may include timeouts or interruptions"]}
