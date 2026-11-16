"""Text, Markdown, JSON and SARIF 2.1.0 renderings of one check run."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
from pathlib import PurePath, PurePosixPath
from urllib.parse import quote

from qawrench import __version__

from .discover import KINDS, Source
from .model import ERROR, INFO, WARNING, Finding, at_or_above, counts
from .registry import AREAS, BY_NAME, INPUTS, Check

FORMAT = "qawrench.check/v1"
REPO_URL = "https://github.com/tianhaogutum/qawrench"
DOCS_URL = f"{REPO_URL}/blob/main/docs/check.md"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
SARIF_LEVEL = {ERROR: "error", WARNING: "warning", INFO: "note"}
LABEL = {ERROR: "error", WARNING: "warn", INFO: "info"}
# GitHub rejects comments over 65,536 characters; keep well below it.
MARKDOWN_ROWS_PER_AREA = 40
MARKDOWN_CELL = 300

# Short rule titles for SARIF; unlisted rules fall back to their check summary.
RULE_TITLES = {
    "parse/invalid-json": "Artifact is not valid JSON",
    "flaky/flaky": "Test failed, then passed on retry",
    "flaky/failed": "Test failed on every attempt",
    "flaky/unexpected-pass": "Test expected to fail passed",
    "flaky/interrupted": "Test was interrupted",
    "flaky/global-errors": "Errors outside any test",
    "har/ambiguous-replay": "Request recorded with different responses",
    "har/volatile-query-key": "Query key changes on every request",
    "trace-budget/over-budget": "Trace archive exceeds the budget",
    "trace-budget/missing-resources": "Trace references missing resources",
    "trace-budget/invalid": "Trace archive does not validate",
    "a11y/new-violation": "New accessibility violation",
    "a11y/violations": "Accessibility violations (no baseline)",
    "lighthouse/noisy": "Lighthouse metric too noisy across runs",
    "lighthouse/runtime-error": "Lighthouse run failed",
    "links/http-failure": "Link returns an HTTP error",
    "links/transport-error": "Link cannot be reached",
    "links/timeout": "Link check timed out",
    "crawlee-export/verification-failed": "Parquet export does not match its manifest",
}


@dataclass
class Run:
    """Everything a renderer needs about one invocation."""

    paths: list[str]
    sources: list[Source]
    scanned: int
    checks: list[Check]
    findings: list[Finding]
    fail_on: str | None  # "error", "warning" or None (never)

    @property
    def failed(self) -> bool:
        return at_or_above(self.findings, self.fail_on)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def summary_line(run: Run) -> str:
    total = counts(run.findings)
    return " · ".join([_plural(len(run.sources), "artifact"), _plural(total[ERROR], "error"),
                       _plural(total[WARNING], "warning"), f"{total[INFO]} info"])


def _area(finding: Finding) -> str:
    check = BY_NAME.get(finding.check)
    return check.area if check else INPUTS


def _by_area(findings) -> dict[str, list[Finding]]:
    groups: dict[str, list[Finding]] = {}
    for finding in findings:
        groups.setdefault(_area(finding), []).append(finding)
    return {area: groups[area] for area in AREAS if area in groups}


def _mix(run: Run) -> str:
    kinds = Counter(source.kind for source in run.sources)

    def label(kind: str) -> str:
        name = KINDS[kind]
        return name if kinds[kind] == 1 or name.endswith("results") else name + "s"

    return ", ".join(f"{kinds[kind]} {label(kind)}" for kind in KINDS if kinds[kind])


# --------------------------------------------------------------------------- text

def _safe(value) -> str:
    """Untrusted labels must not move the cursor or recolour the terminal."""
    return "".join(f"\\x{ord(c):02x}" if ord(c) < 32 or ord(c) == 127 else c for c in str(value))


class _Style:
    def __init__(self, color: bool):
        self.color = color

    def __call__(self, text: str, code: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text


SEVERITY_COLOR = {ERROR: "1;31", WARNING: "33", INFO: "36"}


def render_text(run: Run, *, color: bool = False, verbose: bool = False) -> str:
    style = _Style(color)
    mix = _mix(run)
    lines = [style("qawrench check", "1") + f"  {', '.join(_safe(p) for p in run.paths)}  "
             + style(f"({_plural(len(run.sources), 'artifact')}" + (f": {mix}" if mix else "")
                     + f"; {_plural(run.scanned, 'file')} inspected)", "2")]
    if not run.sources and not run.findings:
        lines += ["", "No test artifacts found (Playwright JSON or CTRF reports, traces, HAR, axe, Pa11y, "
                      "Lighthouse or lychee JSON).",
                  style("Point it at the folder CI uploads, e.g. `qawrench check test-results/`.", "2")]
    labels = {source.display: source.label for source in run.sources}
    findings = run.findings
    width = min(max((len(f.rule_id) for f in findings), default=0), 30)
    for area, group in _by_area(findings).items():
        lines += ["", style(area, "1;4")]
        files: dict[str | None, list[Finding]] = {}
        for finding in group:
            files.setdefault(finding.file, []).append(finding)
        for file in sorted(files, key=lambda name: (name is None, name or "")):
            label = labels.get(file) if file is not None else None
            lines.append("  " + (style(_safe(file), "1") if file is not None else style("(whole run)", "1"))
                         + (style(f"  {label}", "2") if label else ""))
            for finding in files[file]:
                severity = style(LABEL[finding.severity].ljust(5), SEVERITY_COLOR[finding.severity])
                subject = f"{_safe(finding.subject)}: " if finding.subject else ""
                where = (f"line {finding.line}" if finding.line is not None
                         else finding.pointer if verbose and finding.pointer else "")
                lines.append(f"    {severity}  {style(finding.rule_id.ljust(width), '2')}  {subject}{_safe(finding.message)}"
                             + (style(f"  {_safe(where)}", "2") if where else ""))
                if verbose and finding.hint:
                    lines.append(" " * (13 + width) + style(f"-> {_safe(finding.hint)}", "2"))
    if verbose:
        # Cross-file checks (a11y, lighthouse) report on the first artifact of a group,
        # so "no findings of its own" is not the same as "clean": list them only on request.
        mentioned = {finding.file for finding in run.findings}
        quiet = [source for source in run.sources if source.display not in mentioned]
        if quiet:
            lines += ["", style(f"{_plural(len(quiet), 'artifact')} without findings of their own:", "2")]
            lines += [style(f"    {_safe(source.display)}", "2") for source in quiet]
    lines += ["", style(summary_line(run), "1")]
    if run.findings and not verbose:
        lines.append(style("-v adds a fix hint under each finding; --format markdown writes a PR comment.", "2"))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- Markdown

def _cell(value) -> str:
    text = " ".join(str(value).split())
    if len(text) > MARKDOWN_CELL:
        text = text[:MARKDOWN_CELL - 1] + "…"
    # Escape what would end the cell, start HTML or turn into a mention or link.
    for char in ("\\", "|", "<", ">", "`", "*", "_", "[", "]"):
        text = text.replace(char, "\\" + char)
    return text.replace("@", "@\u200b")


def _code(value) -> str:
    text = " ".join(str(value).split()).replace("`", "'").replace("|", "\\|")
    return f"`{text}`"


MD_SEVERITY = {ERROR: "**error**", WARNING: "warning", INFO: "info"}


def render_markdown(run: Run) -> str:
    total = counts(run.findings)
    verdict = ("**Failed**" if run.failed else "**Passed**") + f" (fail on: {run.fail_on or 'never'})"
    lines = ["## qawrench check", "",
             f"{verdict} · {total[ERROR]} errors · {total[WARNING]} warnings · {total[INFO]} info · "
             f"{_plural(len(run.sources), 'artifact')} checked", ""]
    if run.sources:
        lines += [f"Artifacts: {_cell(_mix(run))}", ""]
    else:
        lines += ["No test artifacts found.", ""]
    for area, group in _by_area(run.findings).items():
        important = [f for f in group if f.severity != INFO]
        notes = [f for f in group if f.severity == INFO]
        area_counts = counts(group)
        lines += [f"### {area}", "",
                  f"{area_counts[ERROR]} errors · {area_counts[WARNING]} warnings · {area_counts[INFO]} info", ""]
        if important:
            lines += _md_table(important)
        if notes:
            lines += ["<details><summary>" + _plural(len(notes), "info note") + "</summary>", ""]
            lines += _md_table(notes)
            lines += ["</details>", ""]
    lines += [f"<sub>qawrench {__version__} · read-only, offline · [what each rule means]({DOCS_URL})</sub>", ""]
    return "\n".join(lines)


def _md_table(findings: list[Finding]) -> list[str]:
    rows = ["| Severity | Rule | Artifact | Finding |", "| --- | --- | --- | --- |"]
    for finding in findings[:MARKDOWN_ROWS_PER_AREA]:
        where = finding.file or "(whole run)"
        if finding.line is not None:
            where += f":{finding.line}"
        text = (f"{finding.subject}: " if finding.subject else "") + finding.message
        rows.append(f"| {MD_SEVERITY[finding.severity]} | {_code(finding.rule_id)} | {_code(where)} | {_cell(text)} |")
    if len(findings) > MARKDOWN_ROWS_PER_AREA:
        rows.append(f"| | | | … and {len(findings) - MARKDOWN_ROWS_PER_AREA} more (see `--format json`) |")
    return rows + [""]


# --------------------------------------------------------------------------- JSON

def render_json(run: Run) -> str:
    per_file = Counter(finding.file for finding in run.findings)
    document = {
        "format": FORMAT,
        "tool": {"name": "qawrench", "version": __version__},
        "paths": run.paths,
        "checks": [check.name for check in run.checks],
        "artifacts": [{"path": source.display, "kind": source.kind, "label": source.label,
                       "copies": source.copies, "findings": per_file[source.display]} for source in run.sources],
        "findings": [{**finding.to_dict(), "area": _area(finding)} for finding in run.findings],
        "summary": {"artifacts": len(run.sources), "inspected": run.scanned, **counts(run.findings)},
        "failOn": run.fail_on or "never",
        "failed": run.failed,
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- SARIF

def _uri(path: str) -> dict:
    pure = PurePath(path)
    if pure.is_absolute():
        return {"uri": "file://" + quote(pure.as_posix(), safe="/")}
    return {"uri": quote(PurePosixPath(*pure.parts).as_posix(), safe="/"), "uriBaseId": "%SRCROOT%"}


def _rule(rule_id: str, finding: Finding) -> dict:
    check = BY_NAME.get(finding.check)
    title = RULE_TITLES.get(rule_id) or (check.summary if check else rule_id)
    rule = {"id": rule_id, "name": rule_id.replace("/", "-"), "shortDescription": {"text": title},
            "defaultConfiguration": {"level": SARIF_LEVEL[finding.severity]},
            "helpUri": DOCS_URL,
            "properties": {"check": finding.check, "area": _area(finding)}}
    if check:
        rule["fullDescription"] = {"text": check.summary}
    return rule


def _result(finding: Finding, rule_index: int) -> dict:
    text = (f"{finding.subject}: " if finding.subject else "") + finding.message
    location: dict = {"physicalLocation": {"artifactLocation": _uri(finding.file),
                                           "region": {"startLine": finding.line or 1}}}
    if finding.pointer:
        location["logicalLocations"] = [{"fullyQualifiedName": finding.pointer, "kind": "member"}]
    result = {"ruleId": finding.rule_id, "ruleIndex": rule_index, "level": SARIF_LEVEL[finding.severity],
              "message": {"text": text}, "locations": [location],
              "properties": {"check": finding.check, "severity": finding.severity}}
    if finding.pointer:
        result["properties"]["jsonPointer"] = finding.pointer
    if finding.hint:
        result["properties"]["hint"] = finding.hint
    return result


def render_sarif(run: Run) -> str:
    rules: list[dict] = []
    index: dict[str, int] = {}
    results, notifications = [], []
    for finding in run.findings:
        if finding.file is None:
            # Run-level notices have no artifact; SARIF consumers expect results to have one.
            notifications.append({"level": SARIF_LEVEL[finding.severity],
                                  "message": {"text": f"{finding.rule_id}: {finding.message}"}})
            continue
        if finding.rule_id not in index:
            index[finding.rule_id] = len(rules)
            rules.append(_rule(finding.rule_id, finding))
        results.append(_result(finding, index[finding.rule_id]))
    crashed = any(finding.rule == "internal" for finding in run.findings)
    document = {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "qawrench", "version": __version__, "semanticVersion": __version__,
                                "informationUri": REPO_URL, "rules": rules}},
            "results": results,
            "invocations": [{"executionSuccessful": not crashed, "toolExecutionNotifications": notifications}],
            "columnKind": "unicodeCodePoints",
        }],
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


RENDERERS = {"json": render_json, "sarif": render_sarif, "markdown": render_markdown}
