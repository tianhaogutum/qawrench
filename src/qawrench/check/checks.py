"""The checks. Each one calls the read-only Python API of one qawrench tool.

Per-file checkers are generators ``(source, options) -> Finding...``;
cross-file checkers take ``(sources, options)``. Expected input problems (the
tool's own error type) become findings here; anything else propagates and is
turned into an ``internal`` finding by the registry. Tool packages are imported
inside each checker, so a broken tool or a missing optional dependency only
affects its own check.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import importlib.util
import os
from pathlib import Path
from typing import Iterator

from .discover import DATASET_ITEM, Source
from .model import ERROR, INFO, WARNING, Finding


def _clean(exc: BaseException, source: Source) -> str:
    """Tool messages quote the path they were given; the report already shows it."""
    text = str(exc)
    for path in (str(source.path.absolute()), str(source.path)):
        text = text.replace(f"{path}: ", "").replace(path, source.display)
    return text


def _plural(count: int, word: str, plural: str | None = None) -> str:
    return f"{count} {word if count == 1 else plural or word + 's'}"


def _bytes(value: int) -> str:
    """Decimal (SI) units, as `--trace-budget 25MB` reads them."""
    for unit, size in (("GB", 10 ** 9), ("MB", 10 ** 6), ("kB", 10 ** 3)):
        if value >= size:
            return f"{value / size:.1f} {unit}"
    return f"{value} B"


def _redact_url(url: str) -> str:
    """Query strings and fragments in recordings often carry tokens; never print them."""
    for mark in ("?", "#"):
        if mark in url:
            url = url.split(mark, 1)[0] + mark + "…"
    return url


def available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


# --------------------------------------------------------------------------- flaky

# CTRF has no project field; the flaky tool requires the importer to name one.
CTRF_PROJECT = "ctrf"
FLAKY_HINT = ("keep a history across runs to see whether it keeps happening: "
              "qawrench flaky import --format playwright --run-id RUN --db ledger.sqlite REPORT")


def _test_title(entry: dict, native: bool) -> str:
    titles = " › ".join(title for title in entry["title_path"] if title)
    title = f"{entry['file']} › {titles}" if entry["file"] else titles
    return title + (f" [{entry['project']}]" if native and entry["project"] else "")


def _retries(count: int) -> str:
    return _plural(count, "retry", "retries")


def flaky(source: Source, options) -> Iterator[Finding]:
    if source.kind == "junit":
        yield Finding("flaky", "junit-not-read", INFO,
                      "JUnit XML is not analysed for retries: it does not record individual attempts portably",
                      source.display,
                      hint="add Playwright's JSON reporter (reporter: [['json', { outputFile: 'results.json' }]]) "
                           "or the CTRF reporter; check reads retries from those")
        return
    from qawrench.flaky.adapters import ReportError, parse_report
    from qawrench.flaky.ledger import _classify

    native = source.kind == "playwright-report"
    try:
        raw = source.path.read_bytes()
        parsed = parse_report(raw, "playwright" if native else "ctrf", project=None if native else CTRF_PROJECT)
    except ReportError as exc:
        yield Finding("flaky", "unsupported-report", INFO, f"report not analysed: {_clean(exc, source)}",
                      source.display,
                      hint="flaky reads the Playwright 1.56.1 JSON reporter dialect and "
                           "playwright-ctrf-json-reporter 0.0.29 output, complete reports only")
        return
    tally = Counter()
    for entry in parsed.entries:
        attempts = entry["attempts"]
        outcome = _classify(entry, attempts)
        title = _test_title(entry, native)
        statuses = ", ".join(attempt["status"] for attempt in attempts)
        if entry["retries"]:
            tally["retried"] += 1
        if (outcome["fail_then_pass"] or outcome["mapped_fail_then_pass"]
                or (entry["reported_flaky"] and outcome["passed"])):
            tally["flaky"] += 1
            if entry["attempt_details_unavailable"]:
                message = f"passed after {_retries(entry['retries'])}; reported flaky (earlier attempts not in the report)"
            else:
                message = f"failed, then passed on retry {entry['retries']} (attempts: {statuses})"
            yield Finding("flaky", "flaky", WARNING, message, source.display, subject=title, hint=FLAKY_HINT)
        elif outcome["terminal_failure"]:
            tally["failed"] += 1
            message = (f"failed on all {len(attempts)} attempts ({statuses})" if len(attempts) > 1
                       else "failed (no retries)")
            yield Finding("flaky", "failed", ERROR, message, source.display, subject=title)
        elif outcome["unexpected_pass"]:
            tally["failed"] += 1
            yield Finding("flaky", "unexpected-pass", WARNING,
                          f"passed, but is expected to be {entry['expected_status']} (test.fail())",
                          source.display, subject=title,
                          hint="the bug it marks may be fixed: remove the annotation, or check the test still fails for the right reason")
        elif outcome["interrupted"]:
            tally["interrupted"] += 1
            yield Finding("flaky", "interrupted", WARNING, "interrupted before it finished (cancelled run or global timeout)",
                          source.display, subject=title)
        elif outcome["skipped"] or outcome["not_run"]:
            tally["skipped"] += 1
        else:
            tally["passed"] += 1
    errors = parsed.metadata.get("global_errors") or []
    if errors:
        yield Finding("flaky", "global-errors", WARNING,
                      f"{_plural(len(errors), 'error')} outside any test (global setup, configuration or a worker crash)",
                      source.display, hint="open the report: these failures are not attributed to a test")
    total = len(parsed.entries)
    yield Finding("flaky", "summary", INFO,
                  f"{_plural(total, 'test')}: {tally['passed']} passed, {tally['flaky']} flaky, {tally['failed']} failed, "
                  f"{tally['skipped']} skipped" + (f", {tally['interrupted']} interrupted" if tally["interrupted"] else "")
                  + f"; {tally['retried']} needed retries",
                  source.display)


# --------------------------------------------------------------------------- har

MAX_VOLATILE_KEYS = 8


def _volatile_query_keys(entries: list, url_parts, query_key) -> list[tuple[str, int]]:
    """Query keys whose value differs on every request to the same method and path."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for entry in entries:
        request = entry["request"]
        origin, tail, _ = url_parts(request["url"], "request.url")
        path, sep, query = tail.partition("?")
        if not sep:
            continue
        values: dict[str, list[str]] = {}
        for token in query.split("&"):
            values.setdefault(query_key(token, "request.url"), []).append(token.partition("=")[2])
        groups[(request["method"], origin + path)].append(values)
    stats: Counter = Counter()
    for requests in groups.values():
        if len(requests) < 2:
            continue
        for name in set().union(*requests):
            if not name or not all(name in request for request in requests):
                continue
            seen = [tuple(request[name]) for request in requests]
            if len(set(seen)) == len(seen):
                stats[name] += len(seen)
    return stats.most_common(MAX_VOLATILE_KEYS)


def har(source: Source, options) -> Iterator[Finding]:
    from qawrench.har.core import DoctorError, _query_key, _url_parts, build_plan
    from qawrench.har.fixtures import load_fixture

    try:
        fixture = load_fixture(source.path)
        plan = build_plan(fixture.har, {})
    except (DoctorError, RecursionError) as exc:
        yield Finding("har", "unsupported", INFO, f"not analysed as a replay fixture: {_clean(exc, source)}",
                      source.display,
                      hint="har reads HAR 1.2 with embedded content (Playwright recordHar to a .har file) "
                           "or a self-contained .har.zip")
        return
    entries = fixture.har["log"]["entries"]
    summary = plan["summary"]
    for group in plan["collisionGroups"]:
        if not group["conflict"]:
            continue
        key = group["key"]
        indexes = group["entryIndexes"]
        fields = group["differingResponseFields"] or ["response"]
        differing = f"{', '.join(fields)} {'differs' if len(fields) == 1 else 'differ'}"
        yield Finding("har", "ambiguous-replay", WARNING,
                      f"{key['method']} {_redact_url(key['url'])} recorded {len(indexes)} times with different "
                      f"responses ({differing}); replay serves only one",
                      source.display, pointer=f"/log/entries/{indexes[0]}",
                      hint="entries " + ", ".join(map(str, indexes)) + "; keep one with `qawrench har select-requests`, "
                           "or see every group with `qawrench har plan`")
    for name, count in _volatile_query_keys(entries, _url_parts, _query_key):
        trial = build_plan(fixture.har, {"dropQueryKeys": [name]})["summary"]
        merged = trial["collisionGroups"] > summary["collisionGroups"]
        if not merged or trial["conflictingGroups"] > summary["conflictingGroups"]:
            continue  # the key distinguishes different responses: it is meaningful, not volatile
        yield Finding("har", "volatile-query-key", INFO,
                      f"query key '{name}' changes on every request to the same path ({count} requests); "
                      "a new value at replay time will not match",
                      source.display,
                      hint=f"`qawrench har plan` with rules {{\"dropQueryKeys\": [\"{name}\"]}} merges them "
                           "without conflicting responses")
    yield Finding("har", "summary", INFO,
                  f"{_plural(summary['entries'], 'entry', 'entries')}; "
                  f"{_plural(summary['collisionGroups'], 'request')} recorded more than once, "
                  f"{summary['conflictingGroups']} with different responses",
                  source.display)


# --------------------------------------------------------------------------- trace-budget

TRACE_TOP = 3
# Below this many bytes stored without compression, a repack hint is not worth a line.
STORED_HINT_BYTES = 1_000_000


def trace_budget(source: Source, options) -> Iterator[Finding]:
    from qawrench.trace_budget.archive import CATEGORIES, ArchiveError, inspect_archive

    try:
        report = inspect_archive(source.path, top=TRACE_TOP, budget_bytes=options.trace_budget)
    except ArchiveError as exc:
        text = _clean(exc, source)
        unsupported = "supported" in text
        yield Finding("trace-budget", "unsupported" if unsupported else "invalid", INFO if unsupported else WARNING,
                      f"trace archive {'not analysed' if unsupported else 'does not validate'}: {text}",
                      source.display,
                      hint=None if unsupported else "Trace Viewer may fail to open it; the upload may be truncated")
        return
    stored = report["stored_bytes"] or 1
    allocation = report["allocation"]
    resources = report["resource_allocation"]
    detail = [f"{label} {_bytes(resources[group]['stored_bytes'])}"
              for group, label in (("screencast_screenshots", "screenshots"), ("network_bodies", "network bodies"))
              if resources[group]["stored_bytes"]]
    shares = sorted(((allocation[c]["stored_bytes"], c) for c in CATEGORIES if allocation[c]["stored_bytes"]),
                    reverse=True)
    parts = ", ".join(f"{category} {round(100 * size / stored)}%"
                      + (f" ({', '.join(detail)})" if category == "resources" and detail else "")
                      for size, category in shares)
    message = f"{_bytes(report['archive_bytes'])}, {_plural(report['member_count'], 'member')}: {parts}"
    if source.copies:
        message += f"; {_plural(source.copies, 'identical copy', 'identical copies')} not counted again"
    budget = report["budget"]
    if budget["exceeded"]:
        yield Finding("trace-budget", "over-budget", WARNING,
                      f"{_bytes(budget['measured_bytes'])} exceeds the {_bytes(budget['limit_bytes'])} trace budget "
                      f"by {_bytes(budget['measured_bytes'] - budget['limit_bytes'])}",
                      source.display,
                      hint="record traces only when needed (trace: 'on-first-retry' or 'retain-on-failure'); "
                           "`qawrench trace-budget inspect` lists the largest members")
    missing = report["resource_reference_diagnostics"]
    if missing["missing_occurrences"]:
        roles = missing["missing_occurrences_by_role"]
        kinds = [_plural(count, label, plural) for count, label, plural in (
            (roles["screencast_screenshots"], "screenshot frame", None),
            (roles["network_bodies"], "network body", "network bodies")) if count]
        yield Finding("trace-budget", "missing-resources", WARNING,
                      f"{_plural(missing['missing_occurrences'], 'reference')} to missing resources"
                      + (f" ({', '.join(kinds)})" if kinds else "") + "; Trace Viewer will show gaps",
                      source.display,
                      hint="the trace was cut short or post-processed; `qawrench trace-budget compare` audits removals")
    uncompressed = [m for m in report["members"] if m["compression_method"] == 0 and not m["is_directory"]]
    uncompressed_bytes = sum(m["uncompressed_bytes"] for m in uncompressed)
    if uncompressed_bytes >= STORED_HINT_BYTES:
        yield Finding("trace-budget", "stored-uncompressed", INFO,
                      f"{_plural(len(uncompressed), 'member')} ({_bytes(uncompressed_bytes)}) stored without compression",
                      source.display,
                      hint="`qawrench trace-budget repack` rewrites every member with DEFLATE and verifies each byte; "
                           "savings depend on the content and can be zero")
    yield Finding("trace-budget", "size", INFO, message, source.display)


# --------------------------------------------------------------------------- a11y

# Pa11y CI's combined JSON carries no version; the a11y tool asks the caller to attest one.
PA11Y_VERSION = "4.0.0"
IMPACT_SEVERITY = {"critical": ERROR, "serious": ERROR, "moderate": WARNING, "minor": INFO}
A11Y_BASELINE_HINT = ("save a baseline next to the reports (qawrench a11y baseline --format axe REPORT --out "
                      "a11y-baseline.json) so check reports only new violations")


def _target(target) -> str:
    if isinstance(target, list):
        return " >> ".join(" / ".join(part) if isinstance(part, list) else str(part) for part in target)
    return str(target) or "(page)"


def _a11y_summary(source: Source, pointer, snapshot: dict, have_baselines: bool) -> Iterator[Finding]:
    findings = snapshot["findings"]
    pages = len(snapshot["pages"])
    if not findings:
        yield Finding("a11y", "clean", INFO, f"no violations on {_plural(pages, 'page')}", source.display, pointer=pointer)
        return
    impacts = Counter(finding["severity"] for finding in findings)
    rules = Counter(finding["rule"] for finding in findings).most_common(3)
    spread = ", ".join(f"{impacts[level]} {level}" for level in ("critical", "serious", "moderate", "minor")
                       if impacts[level])
    if impacts[None]:
        spread += (", " if spread else "") + f"{impacts[None]} unrated"
    yield Finding("a11y", "violations", WARNING if impacts["critical"] or impacts["serious"] else INFO,
                  f"{_plural(len(findings), 'violation')} on {_plural(pages, 'page')} ({spread}): "
                  + ", ".join(f"{rule} ×{count}" for rule, count in rules)
                  + "; no baseline to tell new ones from existing ones",
                  source.display, pointer=pointer,
                  hint=("the baselines found cover a different engine, version, page set or options"
                        if have_baselines else A11Y_BASELINE_HINT))


def a11y(sources: list[Source], options) -> Iterator[Finding]:
    from qawrench.a11y.core import BridgeError, compare, make_baseline, validate_baseline

    baselines, current = [], []
    for source in sources:
        if source.kind == "a11y-baseline":
            try:
                baselines.append((source, validate_baseline(source.data)))
            except BridgeError as exc:
                yield Finding("a11y", "invalid-baseline", WARNING, f"baseline not usable: {exc}", source.display,
                              hint="regenerate it from the original report with `qawrench a11y baseline`")
            continue
        reports = source.data if isinstance(source.data, list) else [source.data]
        for index, report in enumerate(reports):
            pointer = f"/{index}" if isinstance(source.data, list) else None
            try:
                if source.kind == "axe":
                    snapshot = make_baseline(report, "axe")
                else:
                    snapshot = make_baseline(report, "pa11y-ci", input_version=PA11Y_VERSION)
            except BridgeError as exc:
                yield Finding("a11y", "unsupported-report", INFO, f"not analysed: {exc}", source.display,
                              pointer=pointer,
                              hint="a11y reads complete axe-core 4.10.3/4.11.0 results objects and Pa11y CI 4 "
                                   "combined JSON; scans with errors or incomplete results are not counted as clean")
                continue
            current.append((source, pointer, snapshot))
    for source, pointer, snapshot in current:
        delta = against = None
        for baseline_source, baseline in baselines:
            try:
                delta = compare(baseline, snapshot)
            except BridgeError:
                continue  # a different engine, version, page set or option set
            against = baseline_source
            break
        if delta is None:
            yield from _a11y_summary(source, pointer, snapshot, bool(baselines))
            continue
        for finding in delta["new"]:
            impact = finding["severity"]
            yield Finding("a11y", "new-violation", IMPACT_SEVERITY.get(impact, WARNING),
                          f"new {impact or 'unrated'} violation {finding['rule']} at {_target(finding['target'])}",
                          source.display, pointer=pointer, subject=finding["page"],
                          hint=finding.get("helpUrl") or None)
        counts = delta["counts"]
        yield Finding("a11y", "baseline-delta", INFO,
                      f"compared with {against.display}: {counts['new']} new, {counts['fixed']} fixed, "
                      f"{counts['unchanged']} unchanged",
                      source.display, pointer=pointer)


# --------------------------------------------------------------------------- lighthouse

# Lab metrics every navigation LHR carries, with their LHR numericUnit.
LH_METRICS = {
    "first-contentful-paint": ("FCP", "millisecond"),
    "largest-contentful-paint": ("LCP", "millisecond"),
    "total-blocking-time": ("TBT", "millisecond"),
    "cumulative-layout-shift": ("CLS", "unitless"),
    "speed-index": ("SI", "millisecond"),
}
# The lighthouse tool requires at least this many runs per side to gate.
LH_MIN_RUNS = 3
# MAD above this share of the median marks a metric as too noisy to compare single runs.
LH_NOISE_RATIO = 0.10


def _lh_value(metric: str, value: float) -> str:
    if LH_METRICS[metric][1] == "unitless":
        return f"{value:.3f}"
    return f"{value / 1000:.2f} s" if value >= 1000 else f"{value:.0f} ms"


def _lh_stat(metric: str, stat: dict, *, spread: bool = False) -> str:
    """``LCP 2.50 s (MAD 100 ms)``; with ``spread`` also the range of the runs."""
    values = [v["value"] for v in stat["values"] if v["status"] == "valid"]
    text = f"{LH_METRICS[metric][0]} {_lh_value(metric, stat['median'])}"
    if len(values) > 1:
        extra = (f", range {_lh_value(metric, min(values))}–{_lh_value(metric, max(values))}" if spread else "")
        text += f" (MAD {_lh_value(metric, stat['mad'])}{extra})"
    return text


def _lh_noisy(stat: dict) -> bool:
    return stat["count"] >= 2 and stat["median"] > 0 and stat["mad"] / stat["median"] > LH_NOISE_RATIO


def lighthouse(sources: list[Source], options) -> Iterator[Finding]:
    from qawrench.lighthouse.core import _project_run, summary

    rules = {metric: {"unit": unit} for metric, (_, unit) in LH_METRICS.items()}
    groups: dict[tuple, list[tuple[Source, dict]]] = {}
    for source in sources:
        run = _project_run(source.data, source.display, rules)
        if run["runtimeError"] is not None:
            yield Finding("lighthouse", "runtime-error", WARNING,
                          "Lighthouse reported a runtimeError for this run; its metrics are not a measurement",
                          source.display, hint="re-run Lighthouse; do not compare or average this run")
            continue
        if run["group_id"] is None:
            errors = run["errors"]
            yield Finding("lighthouse", "unsupported-run", INFO,
                          f"not analysed: {errors[0]}" + (f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""),
                          source.display,
                          hint="lighthouse reads Lighthouse 12.8.2 navigation reports with explicit config settings")
            continue
        for metric, entry in run["metrics"].items():
            if entry["status"] == "invalid":
                yield Finding("lighthouse", "invalid-metric", WARNING,
                              f"{LH_METRICS[metric][0]}: {entry['reason']}", source.display)
        # Runs from different folders (baseline/ vs current/) are never pooled.
        groups.setdefault((str(source.path.parent), run["group_id"]), []).append((source, run))
    for members in groups.values():
        runs = [run for _, run in members]
        first = members[0][0]
        identity = runs[0]["identity"]
        settings = identity.get("configSettings") or {}
        subject = f"{identity['finalDisplayedUrl']} ({settings.get('formFactor')})"
        stats = {metric: summary(runs, metric) for metric in LH_METRICS}
        stats = {metric: stat for metric, stat in stats.items() if stat["count"] and stat["median"] is not None}
        if not stats:
            continue
        where = _plural(len(runs), "run")
        if len(runs) < LH_MIN_RUNS:
            text = "; ".join(_lh_stat(metric, stat) for metric, stat in stats.items())
            yield Finding("lighthouse", "too-few-runs", INFO,
                          f"{where}: {text}; fewer than {LH_MIN_RUNS} runs cannot show how noisy these numbers are",
                          first.display, subject=subject,
                          hint=f"run Lighthouse at least {LH_MIN_RUNS} times per URL (LHCI collect.numberOfRuns) "
                               "and compare medians")
            continue
        noisy = [metric for metric, stat in stats.items() if _lh_noisy(stat)]
        # Noisy metrics first, with the range of the runs; then the others.
        ordered = noisy + [metric for metric in stats if metric not in noisy]
        text = "; ".join(_lh_stat(metric, stats[metric], spread=metric in noisy) for metric in ordered)
        percent = round(LH_NOISE_RATIO * 100)
        if noisy:
            names = ", ".join(LH_METRICS[metric][0] for metric in noisy)
            yield Finding("lighthouse", "noisy", WARNING,
                          f"{where}: {names} {'varies' if len(noisy) == 1 else 'vary'} more than {percent}% "
                          f"of the median; {text}",
                          first.display, subject=subject,
                          hint="a single-run comparison of these metrics is mostly noise; gate on medians with "
                               "`qawrench lighthouse BASELINE_DIR CURRENT_DIR --config gate.json`")
        else:
            yield Finding("lighthouse", "stable", INFO,
                          f"{where}, spread within {percent}% of the median; {text}",
                          first.display, subject=subject,
                          hint="to gate regressions against a baseline: "
                               "`qawrench lighthouse BASELINE_DIR CURRENT_DIR --config gate.json`")


# --------------------------------------------------------------------------- links

LINK_SEVERITY = {"http-failure": ERROR, "transport-error": WARNING, "timeout": WARNING}
SOURCE_PROBLEMS = ("cannot read source", "expected a readable regular source file", "symlink escape")


def _link_roots(source: Source, options) -> list[tuple[Path, str]]:
    if options.source_root is not None:
        return [(Path(options.source_root), str(options.source_root))]
    roots = [(Path("."), ".")]
    parent = os.path.dirname(source.display)
    if parent and os.path.normpath(parent) != ".":
        roots.append((source.path.parent, parent))
    return roots


def links(source: Source, options) -> Iterator[Finding]:
    from qawrench.links.adapter import InputError, convert

    report = source.data
    error_sources = list(report["error_map"])
    markdown = bool(error_sources) and all(name.lower().endswith((".md", ".markdown")) for name in error_sources)
    sarif = problem = None
    for root, root_display in _link_roots(source, options):
        try:
            sarif = convert(report, root=root, markdown=markdown)
            break
        except (InputError, OSError, UnicodeError, RecursionError) as exc:
            problem = exc
            if not any(text in str(exc) for text in SOURCE_PROBLEMS):
                break  # a dialect problem: another root will not help
    if sarif is None:
        located = any(text in str(problem) for text in SOURCE_PROBLEMS)
        broken = sum(value for value in (report.get("errors"), report.get("timeouts")) if type(value) is int)
        if located:
            yield Finding("links", "sources-not-found", WARNING,
                          f"lychee reports {_plural(broken, 'failed link')}, but the files they are in were not found "
                          f"({problem})", source.display,
                          hint="run check from the folder lychee ran in, or pass --source-root DIR")
        else:
            yield Finding("links", "unsupported-report", INFO, f"not analysed: {_clean(problem, source)}",
                          source.display,
                          hint="links reads lychee 0.18 `--format json` output for local files "
                               "(no cache, no redirects counter)")
        return
    run = sarif["runs"][0]
    summary = run["properties"]["summary"]
    for result in run["results"]:
        properties = result["properties"]
        if properties.get("summaryOnly"):
            yield Finding("links", "unsupported-checks", INFO, result["message"]["text"], source.display)
            continue
        category = properties["category"]
        location = result["locations"][0]["physicalLocation"]
        region = location.get("region")
        display = os.path.normpath(os.path.join(root_display, properties["source"]))
        yield Finding("links", category, LINK_SEVERITY.get(category, WARNING), result["message"]["text"], display,
                      line=region["startLine"] if region else None,
                      hint=None if region else "lychee does not say where; the link is somewhere in this file")
    yield Finding("links", "summary", INFO,
                  f"{_plural(summary['inputFindings'], 'failed link')} in "
                  f"{_plural(len([s for s in error_sources if report['error_map'][s]]), 'file')}, "
                  f"{summary['mappedFindings']} located to a line; {summary['ignoredSuccess']} OK, "
                  f"{summary['excluded']} excluded",
                  source.display)


# --------------------------------------------------------------------------- crawlee-export

MANIFEST_SUFFIX = ".manifest.json"


def crawlee_export(source: Source, options) -> Iterator[Finding]:
    if source.kind == "crawlee-dataset":
        items = sum(1 for path in source.path.iterdir() if DATASET_ITEM.fullmatch(path.name))
        yield Finding("crawlee-export", "needs-schema", INFO,
                      f"Crawlee dataset with {_plural(items, 'item')}; not exported: a typed Parquet export needs "
                      "an explicit schema", source.display,
                      hint=f"qawrench crawlee-export {source.display} --schema schema.json --out dataset.parquet")
        return
    parquet = Path(str(source.path)[:-len(MANIFEST_SUFFIX)])
    display = source.display[:-len(MANIFEST_SUFFIX)]
    if not parquet.is_file():
        yield Finding("crawlee-export", "missing-parquet", WARNING,
                      f"manifest without its Parquet file {display}", source.display,
                      hint="upload the .parquet file together with its manifest")
        return
    if not available("pyarrow"):
        yield missing_dependency("crawlee-export", "pyarrow", "parquet", source.display)
        return
    from qawrench.crawlee_export.core import ExportError, verify_export

    try:
        manifest = verify_export(parquet)
    except ExportError as exc:
        yield Finding("crawlee-export", "verification-failed", ERROR,
                      f"{display} does not match its manifest: {_clean(exc, source)}", source.display,
                      hint="the Parquet file was changed, truncated or only partly uploaded; re-run the export")
        return
    yield Finding("crawlee-export", "verified", INFO,
                  f"{display}: {_plural(manifest['row_count'], 'row')} in "
                  f"{_plural(manifest['row_groups'], 'row group')} verified against the manifest "
                  "(schema, row count, digests)", source.display)


def missing_dependency(check: str, module: str, extra: str, file: str | None) -> Finding:
    return Finding(check, "missing-dependency", INFO,
                   f"skipped: the optional dependency {module} is not installed", file,
                   hint=f"pipx install 'qawrench[{extra}]' (or pip install 'qawrench[{extra}]')")
