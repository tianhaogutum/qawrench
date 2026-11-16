"""Conservative comparison of this adapter's own single-run SARIF profile.

Historical source files need not exist. Validate stored evidence structurally,
rebuild canonical results, and never trust a supplied fingerprint as an identity.
"""

from __future__ import annotations

import json
from typing import Any

from .adapter import (
    InputError, _context_bounds, _integer, _keys, _link_result, _object, _occurrence_id,
    _source_lines, _source_name, _string, _unsupported_result, normalize_status,
)


def _without_context(result: dict) -> dict:
    """Remove display-only source context without mutating the supplied report."""
    clean = dict(result)
    if "locations" in result:
        clean["locations"] = []
        for location in result["locations"]:
            location = dict(location)
            location["physicalLocation"] = dict(location["physicalLocation"])
            location["physicalLocation"].pop("contextRegion", None)
            clean["locations"].append(location)
    return clean


def _validate_context(context: Any, region: dict | None) -> None:
    if region is None:
        raise InputError("baseline contextRegion requires a mapped region")
    context = _object(context, "baseline contextRegion")
    _keys(context, {"startLine", "endLine", "snippet"}, set(), "baseline contextRegion")
    for key in ("startLine", "endLine"):
        _integer(context[key], "baseline contextRegion." + key, 1)
    first, last = _context_bounds(region)
    if (context["startLine"], context["endLine"]) != (first, last):
        raise InputError("baseline contextRegion: expected exactly the intersected complete lines")
    snippet = _object(context["snippet"], "baseline contextRegion.snippet")
    _keys(snippet, {"text"}, set(), "baseline contextRegion.snippet")
    lines = _source_lines(_string(snippet["text"], "baseline snippet.text"))
    if (len(lines) != last - first + 1
            or len(lines[-1].rstrip("\r\n")) < region["endColumn"] - 1):
        raise InputError("baseline snippet.text: inconsistent with mapped region")


def _canonical(result: Any) -> dict:
    result = _object(result, "baseline result")
    props = _object(result["properties"], "baseline result.properties")
    category = props["category"]
    annotations = {"baselineMatch", "baselinePreviousRegion"}
    if category == "unsupported":
        _keys(props, {"category", "summaryOnly", "upstreamCount", "mapped"}, annotations,
              "baseline aggregate properties")
        expected = _unsupported_result(_integer(props["upstreamCount"], "baseline upstreamCount", 1))
    else:
        _keys(props, {"category", "source", "originalEntry", "mapped"},
              annotations | {"occurrenceId"}, "baseline result.properties")
        source = _source_name(props["source"], "baseline source")
        entry = _object(props["originalEntry"], "baseline originalEntry")
        _keys(entry, {"url", "status"}, set(), "baseline originalEntry")
        url = _string(entry["url"], "baseline URL")
        if any(c in url for c in "\r\n\x00"):
            raise InputError("baseline URL: expected a single-line non-NUL literal")
        if normalize_status(entry["status"], "baseline status") != category or category not in (
                "http-failure", "transport-error", "timeout"):
            raise InputError("baseline category does not match a supported failure status")
        locations = result["locations"]
        if not isinstance(locations, list) or len(locations) != 1:
            raise InputError("baseline result: expected one location")
        physical = locations[0]["physicalLocation"]
        region = physical.get("region")
        if region is not None:
            _object(region, "baseline region")
            _keys(region, {"startLine", "startColumn", "endLine", "endColumn"}, set(), "baseline region")
            for key, value in region.items():
                _integer(value, "baseline region." + key, 1)
            if region["startLine"] != region["endLine"] or region["endColumn"] <= region["startColumn"]:
                raise InputError("baseline region: expected a nonempty single-line span")
        occurrence_id = None
        if "occurrenceId" in props:
            occurrence_id = _occurrence_id(props["occurrenceId"], "baseline occurrenceId")
            if region is None:
                raise InputError("baseline occurrenceId requires a mapped region")
        expected = _link_result(source, entry, category, region, occurrence_id)
        if "contextRegion" in physical:
            _validate_context(physical["contextRegion"], region)

    # Accept previous comparisons as baselines, ignoring their old annotations.
    # The canonical rebuild also checks artifact URI/base, mapped flag, rule,
    # message and both fingerprints, without opening historical source files.
    clean = {key: value for key, value in _without_context(result).items() if key != "baselineState"}
    clean["properties"] = {key: value for key, value in props.items() if key not in annotations}
    if json.dumps(clean, sort_keys=True, ensure_ascii=False, allow_nan=False) != json.dumps(
            expected, sort_keys=True, ensure_ascii=False, allow_nan=False):
        raise InputError("baseline result does not match this adapter's canonical SARIF evidence")
    return expected


def _region(result: dict) -> dict | None:
    if result["properties"]["category"] == "unsupported":
        return None
    return result["locations"][0]["physicalLocation"].get("region")


def _identity(result: dict) -> tuple:
    props = result["properties"]
    if props["category"] == "unsupported":
        return ("unsupported-summary",)
    pair = (props["source"], props["originalEntry"]["url"])
    if "occurrenceId" in props:
        return ("stable", *pair, props["occurrenceId"])
    return ("position", *pair, json.dumps(_region(result), sort_keys=True))


def compare_baseline(current: dict, baseline: Any) -> None:
    """Annotate current results only; missing baseline results are counts, not fixes."""
    try:
        _object(baseline, "baseline")
        if baseline.get("version") != "2.1.0":
            raise InputError("baseline: expected SARIF 2.1.0")
        runs = baseline["runs"]
        if not isinstance(runs, list) or len(runs) != 1:
            raise InputError("baseline: expected exactly one run")
        before, after = _object(runs[0], "baseline run"), current["runs"][0]
        if before["originalUriBaseIds"] != after["originalUriBaseIds"]:
            raise InputError("baseline source-root mapping does not match current %SRCROOT%; remapping is not supported")
        if (before["tool"] != after["tool"] or before["columnKind"] != after["columnKind"]
                or before["properties"]["inputDialect"] != after["properties"]["inputDialect"]):
            raise InputError("baseline: incompatible tool, column kind or input dialect")
        if before["properties"]["occurrenceMapping"] != after["properties"]["occurrenceMapping"]:
            raise InputError("baseline: incompatible occurrence mapping mode")
        if not isinstance(before["results"], list):
            raise InputError("baseline results: expected an array")
        previous = {}
        positions = set()
        for result in before["results"]:
            canonical = _canonical(result)
            identity = _identity(canonical)
            props = canonical["properties"]
            position = (props.get("source"), props.get("originalEntry", {}).get("url"),
                        json.dumps(_region(canonical), sort_keys=True))
            if identity in previous or position in positions:
                raise InputError("baseline: duplicate occurrence identity or position")
            previous[identity] = canonical
            positions.add(position)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise InputError(f"baseline: invalid or incompatible report: {exc}") from exc

    counts = {"new": 0, "unchanged": 0, "updated": 0, "moved": 0, "absent": 0}
    for result in after["results"]:
        old = previous.pop(_identity(result), None)
        if old is None:
            state = match = "new"
        elif _without_context(result) == old:
            state = match = "unchanged"
        else:
            state = match = "updated"
            if _region(result) != _region(old):
                match = "moved"
                counts["moved"] += 1
                result["properties"]["baselinePreviousRegion"] = dict(_region(old))
        counts[state] += 1
        result["baselineState"] = state
        result["properties"]["baselineMatch"] = match
    counts["absent"] = len(previous)
    after["properties"]["summary"]["baselineComparison"] = counts
