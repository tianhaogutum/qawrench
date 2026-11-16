"""The explicitly supported profile is described in FORMAT.md.

No requests, subprocesses, or URL normalization are involved.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote

from .markdown import link_occurrences


class InputError(ValueError):
    """A report or occurrence cannot be safely and completely represented."""


COUNTERS = (
    "total", "successful", "unknown", "unsupported", "timeouts", "redirects",
    "excludes", "errors", "cached",
)
MAPS = ("success_map", "error_map", "suggestion_map", "excluded_map")
FIELDS = set(COUNTERS + MAPS + ("duration_secs", "detailed_stats"))
RULES = (
    ("http-failure", "LYCHEE-HTTP", "HTTP failure", "A link returned a failed HTTP status."),
    ("timeout", "LYCHEE-TIMEOUT", "Timeout", "A link check timed out."),
    ("transport-error", "LYCHEE-TRANSPORT", "Transport error", "A link check failed without an HTTP response."),
    ("unsupported", "LYCHEE-UNSUPPORTED", "Unsupported check", "Lychee could not check a link type."),
)
RULE_INDEX = {r[0]: n for n, r in enumerate(RULES)}
_UNSET = object()


def _object(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise InputError(f"{where}: expected an object")
    if any(not isinstance(key, str) for key in value):
        raise InputError(f"{where}: object keys must be strings")
    return value


def _keys(value: dict, required: set, optional: set, where: str) -> None:
    missing, extra = required - value.keys(), value.keys() - required - optional
    if missing or extra:
        raise InputError(f"{where}: missing keys {sorted(missing)}; unknown keys {sorted(extra)}")


def _string(value: Any, where: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise InputError(f"{where}: expected {'a' if empty else 'a nonempty'} string")
    # JSON permits escaped unpaired surrogates; UTF-8 files and SARIF do not.
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise InputError(f"{where}: invalid Unicode: {exc}") from exc
    return value


def _integer(value: Any, where: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise InputError(f"{where}: expected an integer >= {minimum}")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError(f"JSON: duplicate object key {key!r}")
        result[key] = value
    return result


def load_json(path: str | Path) -> Any:
    """Load strict UTF-8 JSON, rejecting duplicate keys and non-JSON numbers."""
    def bad_constant(value: str) -> None:
        raise InputError(f"JSON: invalid numeric constant {value}")

    try:
        return json.loads(Path(path).read_text(encoding="utf-8"),
                          object_pairs_hook=_unique_object, parse_constant=bad_constant)
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise InputError(f"{path}: cannot read JSON: {exc}") from exc


def normalize_status(status: Any, where: str = "status") -> str:
    """Classify the tested subset of v0.18.1's human-readable status encoding.

    Success is determined by map membership, not by guessing accepted HTTP codes.
    Unsupported statuses are serializable upstream, but only their aggregate
    counter is present in the supported upstream report profile.
    """
    status = _object(status, where)
    _keys(status, {"text"}, {"code", "details"}, where)
    text = _string(status["text"], where + ".text", empty=True)
    if "code" in status and "details" in status:
        raise InputError(f"{where}: code and details are mutually exclusive in this dialect")
    if "details" in status:
        _string(status["details"], where + ".details", empty=True)
    code = status.get("code")
    if "code" in status:
        _integer(code, where + ".code", 100)
        if code > 999:
            raise InputError(f"{where}.code: expected HTTP code 100..999")
    if text == "Network error":
        return "http-failure" if code is not None else "transport-error"
    if text == "Timeout" and set(status) == {"text"}:
        return "timeout"
    if code is not None and text == f"Timeout ({code})":
        return "timeout"
    if text.startswith("Unsupported: ") and len(text) > len("Unsupported: "):
        if set(status) == {"text"}:
            return "unsupported"
    # Lychee permits --accept overrides: e.g. 404 can be a success.
    if code is not None and re.fullmatch(rf"{code} [^\r\n]+", text):
        return "success"
    if status == {"text": ""}:
        return "excluded"
    raise InputError(f"{where}: unsupported or malformed status {status!r}; see FORMAT.md")


class _Sources:
    """Read-only source snapshot; lexical paths must remain inside the root."""

    def __init__(self, root: str | Path):
        try:
            self.root = Path(root).resolve(strict=True)
            if not self.root.is_dir():
                raise InputError(f"root {root}: expected a directory")
        except (OSError, RuntimeError) as exc:
            raise InputError(f"root {root}: {exc}") from exc
        self.texts: dict[str, str] = {}
        self.original_lines: dict[str, list[str]] = {}
        self.paths: set[Path] = set()

    def read(self, source: Any, where: str) -> str:
        source = _source_name(source, where)
        if source not in self.texts:
            try:
                target = (self.root / source).resolve(strict=True)
                if not target.is_relative_to(self.root):
                    raise InputError(f"{where}: symlink escape outside root: {source!r}")
                if not target.is_file():
                    raise InputError(f"{where}: expected a readable regular source file: {source!r}")
                original = target.read_bytes().decode("utf-8")
                self.original_lines[source] = _source_lines(original)
                # Keep the previous universal-newline mapping contract, while
                # retaining the exact decoded snapshot for optional context.
                self.texts[source] = original.replace("\r\n", "\n").replace("\r", "\n")
                self.paths.add(target)
            except (OSError, UnicodeError, RuntimeError, ValueError) as exc:
                if isinstance(exc, InputError):
                    raise
                raise InputError(f"{where}: cannot read source {source!r}: {exc}") from exc
        return self.texts[source]


def _source_lines(text: str) -> list[str]:
    """Complete original lines, retaining CRLF/LF/CR and no phantom EOF line.

    str.splitlines would also split Unicode separators used as source columns.
    """
    return [line for line in re.findall(r"[^\r\n]*(?:\r\n|\r|\n|$)", text) if line]


def _context_bounds(region: dict) -> tuple[int, int]:
    # Mapped columns are end-exclusive: column 1 of a later line does not
    # intersect that line. Context line numbers themselves are inclusive.
    first, last = region["startLine"], region["endLine"]
    if last > first and region["endColumn"] == 1:
        last -= 1
    return first, last


def _context_region(lines: list[str], region: dict) -> dict:
    first, last = _context_bounds(region)
    return {"startLine": first, "endLine": last,
            "snippet": {"text": "".join(lines[first - 1:last])}}


def _source_name(source: Any, where: str) -> str:
    source = _string(source, where)
    if (source.startswith("/") or "\\" in source or ":" in source
            or any(p in ("", ".", "..") for p in source.split("/"))
            or any(ord(c) < 32 or ord(c) == 127 for c in source)):
        raise InputError(f"{where}: expected a canonical relative POSIX file path inside root: {source!r}")
    return source


def _occurrence_id(value: Any, where: str) -> str:
    value = _string(value, where)
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise InputError(f"{where}: occurrenceId must not contain control characters")
    return value


def _report(report: Any, sources: _Sources) -> list[tuple[str, dict, str]]:
    report = _object(report, "report")
    _keys(report, FIELDS, set(), "report")
    for field in COUNTERS + ("duration_secs",):
        _integer(report[field], "report." + field)
    if type(report["detailed_stats"]) is not bool:
        raise InputError("report.detailed_stats: expected a boolean")
    for name in MAPS:
        _object(report[name], "report." + name)
    if report["suggestion_map"]:
        raise InputError("report.suggestion_map: nonempty suggestions are not supported; retain the original report")
    if report["cached"]:
        raise InputError("report.cached: cached responses are ambiguous in this profile; provide an uncached report")
    if report["unknown"] or report["redirects"]:
        raise InputError("report: unknown/redirect counters have no per-URL records in this dialect; unsupported profile")
    if report["total"] != sum(report[c] for c in COUNTERS[1:-1]):
        raise InputError("report.total: count disagreement with successful+unknown+unsupported+timeouts+redirects+excludes+errors (cached is not additive)")

    findings = []
    counts: Counter = Counter()
    for map_name in ("error_map", "success_map", "excluded_map"):
        for source, entries in report[map_name].items():
            where = f"report.{map_name}[{source!r}]"
            sources.read(source, where)
            if not isinstance(entries, list):
                raise InputError(f"{where}: expected an array")
            seen = set()
            for i, entry in enumerate(entries):
                loc = f"{where}[{i}]"
                _object(entry, loc)
                _keys(entry, {"url", "status"}, set(), loc)
                url = _string(entry["url"], loc + ".url")
                if any(c in url for c in "\r\n\x00"):
                    raise InputError(f"{loc}.url: URL must be a single-line non-NUL literal")
                if url in seen:
                    raise InputError(f"{loc}: duplicate URL in source/map; response multiplicity is unsupported")
                seen.add(url)
                category = normalize_status(entry["status"], loc + ".status")
                if map_name == "error_map":
                    if category not in ("http-failure", "transport-error", "timeout"):
                        raise InputError(f"{loc}: {category} status does not belong in v0.18.1 error_map")
                    counts["timeouts" if category == "timeout" else "errors"] += 1
                    findings.append((source, deepcopy(entry), category))
                else:
                    expected = "success" if map_name == "success_map" else "excluded"
                    if category != expected:
                        raise InputError(f"{loc}: expected {expected} status in {map_name}, got {category}")
                    counts["successful" if expected == "success" else "excludes"] += 1
    for field in ("errors", "timeouts"):
        if report[field] != counts[field]:
            raise InputError(f"report.{field}: count disagreement: counter={report[field]}, error_map={counts[field]}; deduplicated/incomplete reports are unsupported")
    for field in ("successful", "excludes"):
        expected = report[field] if report["detailed_stats"] else 0
        if counts[field] != expected:
            raise InputError(f"report.{field}: count disagreement: map has {counts[field]}, expected {expected} for detailed_stats={report['detailed_stats']}")
    return findings


def _manifest(manifest: Any, findings: list, sources: _Sources) -> dict:
    if not isinstance(manifest, list):
        raise InputError("manifest: expected an array of {source,url,line,column} objects")
    pairs = {(source, entry["url"]) for source, entry, _ in findings}
    occurrences = defaultdict(list)
    seen = set()
    seen_ids = set()
    for i, occurrence in enumerate(manifest):
        where = f"manifest[{i}]"
        _object(occurrence, where)
        _keys(occurrence, {"source", "url", "line", "column"}, {"occurrenceId"}, where)
        text = sources.read(occurrence["source"], where + ".source")
        url = _string(occurrence["url"], where + ".url")
        pair = (occurrence["source"], url)
        if pair not in pairs:
            raise InputError(f"{where}: source/url pair is absent from error_map (URLs match exactly)")
        occurrence_id = None
        if "occurrenceId" in occurrence:
            occurrence_id = _occurrence_id(occurrence["occurrenceId"], where + ".occurrenceId")
            if (*pair, occurrence_id) in seen_ids:
                raise InputError(f"{where}: duplicate source/url/occurrenceId")
            seen_ids.add((*pair, occurrence_id))
        line = _integer(occurrence["line"], where + ".line", 1)
        column = _integer(occurrence["column"], where + ".column", 1)
        identity = (*pair, line, column)
        if identity in seen:
            raise InputError(f"{where}: duplicate source/url/line/column occurrence")
        seen.add(identity)
        # The source snapshot normalizes CRLF and CR for mapping. Only newline is a line break;
        # Unicode separators inside a line remain code points.
        lines = text.split("\n")
        if line > len(lines) or lines[line - 1][column - 1:column - 1 + len(url)] != url:
            raise InputError(f"{where}: URL mismatch at {pair[0]}:{line}:{column}; expected exact {url!r} using Unicode code-point columns")
        occurrences[pair].append(({"startLine": line, "startColumn": column,
                                   "endLine": line, "endColumn": column + len(url)}, occurrence_id))
    return occurrences


def _fingerprint(*identity: Any) -> dict:
    data = json.dumps(identity, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return {"lycheeSourceOccurrence/v1": hashlib.sha256(data.encode("utf-8")).hexdigest()}


def _link_result(source: str, entry: dict, category: str, region: dict | None,
                 occurrence_id: str | None = None) -> dict:
    index = RULE_INDEX[category]
    status = entry["status"]
    detail = status.get("details", "")
    message = f"{entry['url']}: {status['text']}"
    if "code" in status:
        message += f" (HTTP {status['code']})"
    if detail:
        message += f": {detail}"
    physical = {"artifactLocation": {"uri": quote(source, safe="/"), "uriBaseId": "%SRCROOT%"}}
    if region is not None:
        physical["region"] = region
    result = {
        "ruleId": RULES[index][1], "ruleIndex": index, "level": "error",
        "message": {"text": message},
        "locations": [{"physicalLocation": physical}],
        "partialFingerprints": _fingerprint(category, source, entry["url"], region),
        "properties": {"category": category, "source": source,
                       "originalEntry": entry, "mapped": region is not None},
    }
    if occurrence_id is not None:
        result["properties"]["occurrenceId"] = occurrence_id
        result["partialFingerprints"]["lycheeStableOccurrence/v1"] = next(iter(
            _fingerprint(source, entry["url"], occurrence_id).values()))
    return result


def _unsupported_result(count: int) -> dict:
    return {
        "ruleId": "LYCHEE-UNSUPPORTED", "ruleIndex": RULE_INDEX["unsupported"],
        "level": "warning",
        "message": {"text": f"Lychee reports {count} unsupported check(s). This JSON dialect omits their URLs and sources; this is an aggregate, unlocated finding."},
        "partialFingerprints": _fingerprint("unsupported-summary"),
        "properties": {"category": "unsupported", "summaryOnly": True,
                       "upstreamCount": count, "mapped": False},
    }


def convert(report: Any, *, root: str | Path = ".", manifest: Any = _UNSET,
            markdown: bool = False, baseline: Any = _UNSET,
            source_context: bool = False,
            output_path: str | Path | None = None,
            protected_paths: tuple[str | Path, ...] = ()) -> dict:
    """Validate all input, then return a deterministic SARIF 2.1.0 object.

    No writes occur. If output_path is supplied, reject overwriting any read
    source or protected input (CLI supplies report/manifest/baseline paths).
    markdown opts into the bounded Markdown reader instead of literal manifest
    evidence. Sources and report URLs retain their original identities.
    baseline accepts a previous adapter SARIF object with the same resolved root
    and mapping mode. Manifest occurrenceId values allow explicit move tracking.
    source_context adds exact complete original lines to mapped locations only;
    this may disclose surrounding source text and does not affect identity.
    """
    if type(markdown) is not bool:
        raise InputError("markdown: expected a boolean")
    if type(source_context) is not bool:
        raise InputError("source_context: expected a boolean")
    if markdown and manifest is not _UNSET:
        raise InputError("markdown and manifest are mutually exclusive")
    sources = _Sources(root)
    findings = _report(report, sources)
    if markdown:
        occurrences = defaultdict(list)
        pairs = {(source, entry["url"]) for source, entry, _ in findings}
        for source in sorted({source for source, _ in pairs}):
            for url, region in link_occurrences(sources.texts[source]):
                if (source, url) in pairs:
                    occurrences[source, url].append((region, None))
    else:
        occurrences = _manifest([] if manifest is _UNSET else manifest, findings, sources)
    if output_path is not None:
        try:
            output = Path(output_path).resolve()
            protected = sources.paths | {Path(p).resolve() for p in protected_paths}
            # Also reject hard-link aliases, even though the writer uses replace.
            if output in protected or (output.exists() and any(output.samefile(p) for p in protected)):
                raise InputError(f"output {output_path}: would overwrite a source or input file")
        except (OSError, RuntimeError) as exc:
            raise InputError(f"output {output_path}: {exc}") from exc

    results = []
    mapped_findings = mapped_results = unmapped_findings = 0
    for source, entry, category in sorted(findings, key=lambda f: (f[0], f[1]["url"])):
        regions = occurrences.get((source, entry["url"]), [])
        if regions:
            mapped_findings += 1
            mapped_results += len(regions)
        else:
            unmapped_findings += 1
        for region, occurrence_id in sorted(regions, key=lambda r: (r[0]["startLine"], r[0]["startColumn"])) or [(None, None)]:
            result = _link_result(source, entry, category, region, occurrence_id)
            if source_context and region is not None:
                result["locations"][0]["physicalLocation"]["contextRegion"] = _context_region(
                    sources.original_lines[source], region)
            results.append(result)
    unsupported = report["unsupported"]
    if unsupported:
        results.append(_unsupported_result(unsupported))
    summary = {
        "inputErrors": report["errors"], "inputTimeouts": report["timeouts"],
        "inputFindings": len(findings), "mappedFindings": mapped_findings,
        "mappedResults": mapped_results, "unmappedFindings": unmapped_findings,
        "ignoredSuccess": report["successful"], "excluded": report["excludes"],
        "unsupportedChecks": unsupported, "aggregateResults": int(bool(unsupported)),
        "resultCount": len(results),
    }
    root_uri = sources.root.as_uri()
    sarif = {
        "$schema": "https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/schemas/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "lychee-sarif-map", "version": "0.1.0",
                                "rules": [{"id": rule_id, "name": title.replace(" ", ""),
                                           "shortDescription": {"text": title},
                                           "fullDescription": {"text": description}}
                                          for _, rule_id, title, description in RULES]}},
            "columnKind": "unicodeCodePoints", "results": results,
            "originalUriBaseIds": {"%SRCROOT%": {"uri": root_uri if root_uri.endswith("/") else root_uri + "/"}},
            "properties": {"summary": summary, "inputDialect": "lychee-v0.18.1-strict-subset",
                           "occurrenceMapping": "markdown-bounded-v1" if markdown else "literal-manifest",
                           "lycheeCounters": {c: report[c] for c in COUNTERS},
                           "durationSecs": report["duration_secs"],
                           "detailedStats": report["detailed_stats"]},
        }],
    }
    if baseline is not _UNSET:
        from .baseline import compare_baseline
        compare_baseline(sarif, baseline)
    return sarif
