"""The list of checks, and the crash-isolating loop that runs them."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Callable, Iterable

from . import checks
from .discover import Discovery, Source
from .model import ERROR, INFO, Finding

ISSUES_URL = "https://github.com/tianhaogutum/qawrench/issues"
# Set to 1 to let a crashing check raise its traceback instead of reporting it.
DEBUG_ENV = "QAWRENCH_CHECK_DEBUG"

TESTS = "Tests & flakiness"
A11Y_PERF = "Accessibility & performance"
PAGES = "Pages & links"
CRAWLING = "Crawling"
INPUTS = "Inputs"
AREAS = (TESTS, A11Y_PERF, PAGES, CRAWLING, INPUTS)


@dataclass(frozen=True)
class CheckOptions:
    # Trace archive budget in bytes (archive size on disk); None: report sizes only.
    trace_budget: int | None = None
    # Folder lychee ran in; None tries the current directory, then the report's folder.
    source_root: Path | None = None


@dataclass(frozen=True)
class Check:
    name: str
    area: str
    summary: str
    kinds: frozenset[str]
    # Per-file checks take (source, options); cross-file checks take (sources, options).
    run: Callable[..., Iterable[Finding]] | None = None
    cross_file: bool = False
    rules: tuple[str, ...] = field(default=())
    # Top-level import names of optional dependencies, and the extra that installs them.
    requires: tuple[str, ...] = ()
    extra: str | None = None
    # For checks that need input `check` cannot guess: what to run instead.
    needs_config: str | None = None

    @property
    def tool(self) -> str:
        return "-" if self.name == "parse" else self.name


CHECKS: tuple[Check, ...] = (
    Check("flaky", TESTS,
          "Playwright JSON / CTRF reports: tests that failed then passed on retry, failed on every attempt, "
          "passed unexpectedly or were interrupted",
          frozenset({"playwright-report", "ctrf", "junit"}), checks.flaky,
          rules=("flaky", "failed", "unexpected-pass", "interrupted", "global-errors", "summary",
                 "unsupported-report", "junit-not-read")),
    Check("har", TESTS,
          "HAR recordings: requests recorded several times with different responses (ambiguous replay), "
          "query keys whose value changes on every request",
          frozenset({"har", "har-zip"}), checks.har,
          rules=("ambiguous-replay", "volatile-query-key", "summary", "unsupported")),
    Check("trace-budget", TESTS,
          "Playwright trace.zip files: size by category, --trace-budget overruns, references to missing "
          "screenshots or network bodies",
          frozenset({"trace"}), checks.trace_budget,
          rules=("over-budget", "missing-resources", "stored-uncompressed", "size", "invalid", "unsupported")),
    Check("a11y", A11Y_PERF,
          "axe-core / Pa11y CI results: new violations against a baseline found next to them, "
          "otherwise a summary by impact",
          frozenset({"axe", "pa11y-ci", "a11y-baseline"}), checks.a11y, cross_file=True,
          rules=("new-violation", "baseline-delta", "violations", "clean", "invalid-baseline", "unsupported-report")),
    Check("lighthouse", A11Y_PERF,
          "Lighthouse JSON (LHR): median and spread of FCP, LCP, TBT, CLS and Speed Index over repeated runs; "
          "failed runs",
          frozenset({"lhr"}), checks.lighthouse, cross_file=True,
          rules=("noisy", "stable", "too-few-runs", "runtime-error", "invalid-metric", "unsupported-run")),
    Check("links", PAGES,
          "lychee JSON: failed links mapped to the file (and, for Markdown, the line) that contains them",
          frozenset({"lychee"}), checks.links,
          rules=("http-failure", "transport-error", "timeout", "unsupported-checks", "summary",
                 "sources-not-found", "unsupported-report")),
    Check("page-diff", PAGES, "Compare two saved HTML snapshots with explicit noise rules",
          frozenset(), requires=("bs4", "soupsieve"), extra="html",
          needs_config="needs a before/after pair and a rules file: qawrench page-diff BEFORE.html AFTER.html "
                       "--rules rules.json"),
    Check("structured-data", PAGES, "Compare JSON-LD and OpenGraph between two saved HTML captures",
          frozenset(), requires=("extruct", "lxml"), extra="structured",
          needs_config="needs two captures of the same page and its URL: qawrench structured-data RAW.html "
                       "RENDERED.html --base-url https://..."),
    Check("crawlee-export", CRAWLING,
          "Parquet exports with a manifest: verified against it (schema, row count, digests); "
          "Crawlee datasets are listed",
          frozenset({"parquet-export", "crawlee-dataset"}), checks.crawlee_export,
          rules=("verification-failed", "missing-parquet", "verified", "needs-schema", "missing-dependency"),
          requires=("pyarrow",), extra="parquet"),
    Check("frontier", CRAWLING, "Simulate crawl request deduplication under a URL policy",
          frozenset(), requires=("w3lib",), extra="frontier",
          needs_config="needs a request list and a policy: qawrench frontier requests.jsonl --policy policy.json "
                       "--dry-run"),
    Check("parse", INPUTS, "Artifacts that cannot be read or parsed",
          frozenset(), rules=("invalid-json", "invalid-encoding", "unreadable", "unrecognised")),
)
BY_NAME = {check.name: check for check in CHECKS}


def select(only: Iterable[str] = (), skip: Iterable[str] = ()) -> list[Check]:
    """Checks to run, in registry order. Raises ValueError for unknown names."""
    only, skip = list(only), list(skip)
    unknown = sorted({name for name in [*only, *skip] if name not in BY_NAME})
    if unknown:
        raise ValueError(f"unknown check(s): {', '.join(unknown)} (see --list-checks)")
    return [check for check in CHECKS if (not only or check.name in only) and check.name not in skip]


def _internal(check: Check, exc: BaseException, source: Source | None) -> Finding:
    # Only the exception type: messages of unexpected errors may quote artifact content.
    return Finding(check.name, "internal", ERROR,
                   f"the {check.name} check crashed ({type(exc).__name__}); its results are missing, "
                   "other checks still ran", source.display if source else None,
                   hint=f"please report it at {ISSUES_URL} (set {DEBUG_ENV}=1 for the traceback)")


def _guarded(check: Check, args: tuple, source: Source | None) -> list[Finding]:
    try:
        return list(check.run(*args))
    except ModuleNotFoundError as exc:
        root = (exc.name or "").partition(".")[0]
        if root in check.requires and check.extra:
            return [checks.missing_dependency(check.name, root, check.extra, source.display if source else None)]
        if os.environ.get(DEBUG_ENV) == "1":
            raise
        return [_internal(check, exc, source)]
    except Exception as exc:  # noqa: BLE001 - one broken checker must not abort the run
        if os.environ.get(DEBUG_ENV) == "1":
            raise
        return [_internal(check, exc, source)]


def run_checks(discovery: Discovery, selected: Iterable[Check] | None = None,
               options: CheckOptions | None = None, requested: Iterable[str] = ()) -> list[Finding]:
    """Run ``selected`` checks (default: all) over discovered sources; never raises for a checker bug.

    ``requested`` names checks the user asked for explicitly (``--only``): a
    check that needs configuration then explains how to run it instead of
    staying silent.
    """
    selected = list(CHECKS if selected is None else selected)
    options = options or CheckOptions()
    requested = set(requested)
    names = {check.name for check in selected}
    findings = [finding for finding in discovery.findings if finding.check in names]
    for check in selected:
        if check.run is None:
            if check.needs_config and check.name in requested:
                findings.append(Finding(check.name, "needs-config", INFO,
                                        f"not run by check: it {check.needs_config}"))
            continue
        relevant = [source for source in discovery.sources if source.kind in check.kinds]
        if check.cross_file:
            if relevant:
                findings.extend(_guarded(check, (relevant, options), None))
            continue
        for source in relevant:
            findings.extend(_guarded(check, (source, options), source))
    return sorted(findings, key=Finding.sort_key)
