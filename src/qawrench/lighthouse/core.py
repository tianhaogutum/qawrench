"""Strict input projection, comparable groups, and a deterministic MAD policy.

MAD here is the raw median absolute deviation, not an estimate of standard
error, a confidence interval, or a statistical significance test.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from qawrench import __version__

from .confidence import add_confidence, apply_confidence_gate, validate_confidence_level

SUPPORTED_VERSIONS = frozenset({"12.8.2"})
EXIT_CODES = {"pass": 0, "regression": 1, "invalid": 2, "inconclusive": 3}


def finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _pairs(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _check_finite(value: Any) -> None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not finite_number(value):
            raise ValueError("non-finite or unrepresentable JSON number")
    elif isinstance(value, dict):
        for item in value.values():
            _check_finite(item)
    elif isinstance(value, list):
        for item in value:
            _check_finite(item)


def load_json(path: str | Path) -> Any:
    """Read UTF-8 strict JSON; reject duplicate keys and non-finite numbers."""
    value = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_pairs,
                       parse_constant=_constant)
    _check_finite(value)
    return value


def _keys(obj: Any, required: set[str], name: str) -> None:
    if not isinstance(obj, dict):
        raise ValueError(f"{name} must be an object")
    missing, unknown = required - obj.keys(), obj.keys() - required
    if missing or unknown:
        raise ValueError(f"{name}: missing keys {sorted(missing)}, unknown keys {sorted(unknown)}")


def validate_policy(policy: Any) -> dict:
    """Validate the complete policy; no implicit metric direction or tolerances."""
    _keys(policy, {"schema_version", "min_samples", "noise_multiplier", "metrics"}, "config")
    if type(policy["schema_version"]) is not int or policy["schema_version"] != 1:
        raise ValueError("config.schema_version must be 1")
    if type(policy["min_samples"]) is not int or policy["min_samples"] < 3:
        raise ValueError("config.min_samples must be an integer >= 3")
    if not finite_number(policy["noise_multiplier"]) or policy["noise_multiplier"] < 0:
        raise ValueError("config.noise_multiplier must be finite and >= 0")
    if not isinstance(policy["metrics"], dict) or not policy["metrics"]:
        raise ValueError("config.metrics must be a nonempty object keyed by audit ID")
    for metric, rule in policy["metrics"].items():
        if not isinstance(metric, str) or not metric.strip():
            raise ValueError("metric ID must be a nonempty string")
        _keys(rule, {"direction", "unit", "absolute_tolerance", "relative_tolerance"}, metric)
        if rule["direction"] not in ("lower", "higher"):
            raise ValueError(f"{metric}.direction must be lower or higher")
        if not isinstance(rule["unit"], str) or not rule["unit"].strip():
            raise ValueError(f"{metric}.unit must be a nonempty numericUnit string")
        for field in ("absolute_tolerance", "relative_tolerance"):
            if not finite_number(rule[field]) or rule[field] < 0:
                raise ValueError(f"{metric}.{field} must be finite and >= 0")
    return policy


def _url(value: Any) -> bool:
    if not isinstance(value, str) or any(c.isspace() for c in value):
        return False
    try:
        parts = urlsplit(value)
        return parts.scheme in ("http", "https") and bool(parts.hostname) and parts.port != 0
    except ValueError:
        return False


def _metadata(lhr: dict) -> tuple[dict, list[str]]:
    """Preserve and compare every config setting, without guessing defaults."""
    errors = []
    version = lhr.get("lighthouseVersion")
    if not isinstance(version, str) or version not in SUPPORTED_VERSIONS:
        errors.append(f"unsupported lighthouseVersion {version!r}; supported: {sorted(SUPPORTED_VERSIONS)}")
    for field in ("finalUrl", "finalDisplayedUrl"):
        if not _url(lhr.get(field)):
            errors.append(f"{field} must be an explicit absolute HTTP(S) URL")
    if lhr.get("gatherMode") != "navigation":
        errors.append("only gatherMode=navigation is supported")
    settings = lhr.get("configSettings")
    if not isinstance(settings, dict):
        errors.append("configSettings must be an object")
    else:
        if settings.get("formFactor") not in ("mobile", "desktop"):
            errors.append("configSettings.formFactor must be mobile or desktop")
        if settings.get("throttlingMethod") not in ("simulate", "devtools", "provided"):
            errors.append("configSettings.throttlingMethod must be simulate, devtools or provided")
        throttling = settings.get("throttling")
        throttle_fields = ("rttMs", "throughputKbps", "requestLatencyMs", "downloadThroughputKbps",
                           "uploadThroughputKbps", "cpuSlowdownMultiplier")
        if not isinstance(throttling, dict):
            errors.append("configSettings.throttling must be an object with explicit numeric settings")
        else:
            for field in throttle_fields:
                value = throttling.get(field)
                if not finite_number(value) or value < 0 or (field == "cpuSlowdownMultiplier" and value == 0):
                    errors.append(f"configSettings.throttling.{field} must be explicit, finite and nonnegative (CPU > 0)")
        screen = settings.get("screenEmulation")
        if not isinstance(screen, dict):
            errors.append("configSettings.screenEmulation must be an explicit object")
        else:
            for field in ("width", "height", "deviceScaleFactor"):
                value = screen.get(field)
                if not finite_number(value) or value < 0:
                    errors.append(f"configSettings.screenEmulation.{field} must be explicit, finite and >= 0")
            for field in ("disabled", "mobile"):
                if type(screen.get(field)) is not bool:
                    errors.append(f"configSettings.screenEmulation.{field} must be an explicit boolean")
    environment = lhr.get("environment")
    if not isinstance(environment, dict):
        environment = {}
    agents = {"userAgent": lhr.get("userAgent"),
              "hostUserAgent": environment.get("hostUserAgent"),
              "networkUserAgent": environment.get("networkUserAgent")}
    for field, value in agents.items():
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{field} must be an explicit nonempty string")
    return {"finalUrl": lhr.get("finalUrl"), "finalDisplayedUrl": lhr.get("finalDisplayedUrl"),
            "lighthouseVersion": version, "gatherMode": lhr.get("gatherMode"),
            "configSettings": settings, **agents}, errors


def _read_run(path: Path, rule_metrics: dict) -> dict:
    try:
        lhr = load_json(path)
        if not isinstance(lhr, dict):
            raise ValueError("LHR must be a JSON object, not a manifest, list or wrapper")
    except (OSError, ValueError, RecursionError) as exc:
        run: dict = {"file": path.name, "group_id": None, "errors": [], "metrics": {}}
        run["errors"].append(f"cannot read LHR: {exc}")
        for metric in rule_metrics:
            run["metrics"][metric] = {"status": "invalid", "value": None, "reason": "unreadable LHR"}
        return run
    return _project_run(lhr, path.name, rule_metrics)


def _project_run(lhr: dict, filename: str, rule_metrics: dict) -> dict:
    """Apply identical validation to directory LHRs and saved snapshots."""
    run: dict = {"file": filename, "group_id": None, "errors": [], "metrics": {}}
    identity, metadata_errors = _metadata(lhr)
    run["identity"] = identity
    run["errors"].extend(metadata_errors)
    if not metadata_errors:
        run["group_id"] = hashlib.sha256(canonical(identity).encode()).hexdigest()[:16]
    run["runtimeError"] = lhr.get("runtimeError")
    if lhr.get("runtimeError") is not None:
        run["errors"].append("LHR runtimeError: run is invalid")
    run["runWarnings"] = lhr.get("runWarnings", [])
    run["benchmarkIndex"] = (lhr.get("environment") or {}).get("benchmarkIndex") if isinstance(lhr.get("environment"), dict) else None
    audits = lhr.get("audits")
    if not isinstance(audits, dict):
        run["errors"].append("audits must be an object")
        audits = {}
    for metric, rule in rule_metrics.items():
        audit = audits.get(metric)
        value = audit.get("numericValue") if isinstance(audit, dict) else None
        entry = {"value": value, "status": "valid", "reason": "numericValue", "unit": None}
        if metric not in audits or audit is None:
            entry.update(status="missing", reason="audit missing or null")
        elif not isinstance(audit, dict):
            entry.update(status="invalid", reason="audit must be an object")
        else:
            entry["unit"] = audit.get("numericUnit")
            if audit.get("id") != metric:
                entry.update(status="invalid", reason="audit.id must match selected metric key")
            elif audit.get("scoreDisplayMode") == "error" or audit.get("errorMessage"):
                entry.update(status="invalid", reason=f"audit error: {audit.get('errorMessage', 'scoreDisplayMode=error')}")
            elif value is None:
                entry.update(status="missing", reason="numericValue missing or null; score is never substituted")
            elif not finite_number(value):
                entry.update(status="invalid", reason="numericValue must be a finite number, not a boolean/string")
            elif audit.get("numericUnit") != rule["unit"]:
                entry.update(status="invalid", reason=f"numericUnit must be {rule['unit']!r}")
        run["metrics"][metric] = entry
        if entry["status"] == "invalid":
            run["errors"].append(f"{metric}: {entry['reason']}")
    return run


def median(values: list[float]) -> float:
    """Median with an overflow-safe average for even sample counts."""
    values = sorted(values)
    n = len(values)
    return float(values[n // 2]) if n % 2 else values[n // 2 - 1] / 2 + values[n // 2] / 2


def summary(runs: list[dict], metric: str) -> dict:
    values = [run["metrics"][metric]["value"] for run in runs
              if run["metrics"][metric]["status"] == "valid"]
    center = median(values) if values else None
    mad = median([abs(v - center) for v in values]) if values else None
    overflow = any(v is not None and not finite_number(v) for v in (center, mad))
    return {"run_count": len(runs), "count": len(values),
            "median": None if overflow else center, "mad": None if overflow else mad,
            "statistics_error": "numeric overflow in statistics" if overflow else None,
            "values": [{"file": r["file"], **r["metrics"][metric]} for r in runs]}


def _metric_result(baseline: list[dict], current: list[dict], metric: str, rule: dict, policy: dict) -> dict:
    result: dict = {"audit": metric, "direction": rule["direction"], "unit": rule["unit"],
                    "baseline": summary(baseline, metric), "current": summary(current, metric),
                    "decision": "inconclusive", "reasons": [], "delta": None,
                    "relative_delta": None, "relative_delta_reason": None,
                    "adverse_delta": None, "tolerance": None, "noise_margin": None,
                    "regression_boundary": None}
    stats = (result["baseline"], result["current"])
    reasons = result["reasons"]
    if any(s["statistics_error"] for s in stats):
        result["decision"] = "invalid"
        reasons.append("numeric overflow in statistics; rescale or correct the inputs")
    if any(r["errors"] for r in baseline + current):
        result["decision"] = "invalid"
        reasons.append("one or more runs have invalid input; see run errors")
    if any(v["status"] == "missing" for s in stats for v in s["values"]):
        reasons.append("missing/null metric in one or more runs; runs are not silently dropped")
    if any(s["count"] < policy["min_samples"] for s in stats):
        reasons.append(f"at least {policy['min_samples']} valid samples required on each side")
    if all(s["median"] is not None for s in stats):
        b, c = (s["median"] for s in stats)
        delta = c - b
        relative = delta / abs(b) if b else None
        tolerance = max(rule["absolute_tolerance"], abs(b) * rule["relative_tolerance"])
        margin = policy["noise_multiplier"] * max(s["mad"] for s in stats)
        adverse = delta if rule["direction"] == "lower" else -delta
        result.update(delta=delta, relative_delta=relative, adverse_delta=adverse,
                      tolerance=tolerance, noise_margin=margin, regression_boundary=tolerance + margin)
        if b == 0:
            result["relative_delta_reason"] = "baseline median is zero; relative delta undefined; absolute tolerance applies"
        try:
            _check_finite(result)
        except ValueError:
            for key in ("delta", "relative_delta", "adverse_delta", "tolerance", "noise_margin", "regression_boundary"):
                result[key] = None
            # Medians/MAD themselves can overflow for extreme opposite-signed data.
            for side in stats:
                for key in ("median", "mad"):
                    if side[key] is not None and not finite_number(side[key]):
                        side[key] = None
            result["decision"] = "invalid"
            reasons.append("numeric overflow in derived statistics; rescale or correct the inputs")
        if not reasons:
            if adverse + margin <= tolerance:
                result["decision"] = "pass"
                reasons.append("adverse median delta + noise margin <= tolerance")
            elif adverse > tolerance + margin:
                result["decision"] = "regression"
                reasons.append("adverse median delta > tolerance + noise margin")
            else:
                result["decision"] = "inconclusive"
                reasons.append("noise band overlaps the tolerance boundary; neither pass nor regression is established by this policy")
    return result


def aggregate(decisions: list[str]) -> str:
    for state in ("invalid", "regression", "inconclusive"):
        if state in decisions:
            return state
    return "pass" if decisions else "invalid"


def _iter_identity_changes(baseline: dict, current: dict):
    """Yield JSON-pointer differences in sorted raw-key depth-first order."""
    def visit(b: Any, c: Any, path: str, b_present: bool = True, c_present: bool = True):
        if b_present and c_present:
            if isinstance(b, dict) and isinstance(c, dict):
                for key in sorted(b.keys() | c.keys()):
                    token = key.replace("~", "~0").replace("/", "~1")
                    yield from visit(b.get(key), c.get(key), path + "/" + token, key in b, key in c)
                return
            # Python equality merges false/0 and 4/4.0, unlike group hashing.
            if canonical(b) == canonical(c):
                return
        yield {"path": path, "baseline_present": b_present, "baseline": b,
               "current_present": c_present, "current": c}

    yield from visit(baseline, current, "")


def _identity_changes(baseline: dict, current: dict) -> list[dict]:
    """Lossless JSON-pointer differences under the exact group identity rules."""
    return list(_iter_identity_changes(baseline, current))


def _bounded_identity_changes(baseline: dict, current: dict, limit: int) -> tuple[list[dict], dict]:
    """Count every change while retaining only the requested prefix."""
    changes = []
    total = 0
    for change in _iter_identity_changes(baseline, current):
        total += 1
        if len(changes) < limit:
            changes.append(change)
    emitted = len(changes)
    return changes, {"limit": limit, "total": total, "emitted": emitted,
                     "omitted": total - emitted, "truncated": emitted < total}


def _profile_mismatches(grouped: dict[str, dict], limit: int | None, field_limit: int | None = None) -> tuple[list[dict], int, list[dict]]:
    """Count all candidates, then diff only the requested prefix in group-ID order."""
    anchors: dict[tuple[str, str, str], dict[str, list[dict]]] = {}
    baseline_groups = []
    for key in sorted(grouped):
        group = grouped[key]
        if bool(group["baseline"]) == bool(group["current"]):
            continue
        identity = group["identity"]
        anchor = (identity["finalUrl"], identity["finalDisplayedUrl"], identity["configSettings"]["formFactor"])
        bucket = anchors.setdefault(anchor, {"baseline": [], "current": []})
        bucket["baseline" if group["baseline"] else "current"].append(group)
        if group["baseline"]:
            baseline_groups.append((anchor, group))
    total = sum(len(bucket["baseline"]) * len(bucket["current"]) for bucket in anchors.values())
    counts = {}
    if limit is not None:
        for bucket in anchors.values():
            for side, opposite in (("baseline", "current"), ("current", "baseline")):
                for group in bucket[side]:
                    counts[group["id"]] = {"group_id": group["id"], "side": side,
                                           "total": len(bucket[opposite]), "emitted": 0}
    remaining = total if limit is None else min(limit, total)
    mismatches = []
    # Both lists inherit sorted group IDs; do not build the Cartesian product to sort/slice it.
    for (url, displayed, device), baseline in baseline_groups:
        if not remaining:
            break
        for current in anchors[(url, displayed, device)]["current"]:
            if not remaining:
                break
            pair = {"baseline_group_id": baseline["id"], "current_group_id": current["id"],
                    "finalUrl": url, "finalDisplayedUrl": displayed, "formFactor": device}
            if field_limit is None:
                pair["changes"] = _identity_changes(baseline["identity"], current["identity"])
            else:
                pair["changes"], pair["changes_summary"] = _bounded_identity_changes(
                    baseline["identity"], current["identity"], field_limit)
            mismatches.append(pair)
            if limit is not None:
                counts[baseline["id"]]["emitted"] += 1
                counts[current["id"]]["emitted"] += 1
            remaining -= 1
    for row in counts.values():
        row["omitted"] = row["total"] - row["emitted"]
        row["truncated"] = row["omitted"] > 0
    return mismatches, total, [counts[key] for key in sorted(counts)]


def compare(baseline_dir: str | Path, current_dir: str | Path, policy: dict, *,
            confidence_level: float | None = None, confidence_gate: bool = False,
            mismatch_limit: int | None = None, mismatch_field_limit: int | None = None) -> dict:
    """Compare a baseline directory or saved manifest to immediate current LHRs.

    Optional confidence_level adds exact median-change diagnostics. Explicit
    confidence_gate uses those intervals for absolute-only tolerance decisions;
    otherwise the MAD gate and exit code remain unchanged. Invalid configuration or saved
    manifests raise ValueError. Directory data failures are returned in the
    report to preserve every readable run and every group decision.
    Positive mismatch_limit bounds diagnostic candidate records only, adding
    explicit total/emitted/omitted counts globally and per unmatched group;
    Positive mismatch_field_limit bounds change records per emitted candidate,
    adding per-pair counts. None preserves unlimited output for either option.
    """
    validate_policy(policy)
    validate_confidence_level(confidence_level)
    if mismatch_limit is not None and (type(mismatch_limit) is not int or mismatch_limit <= 0):
        raise ValueError("mismatch_limit must be a positive integer or None")
    if mismatch_field_limit is not None and (type(mismatch_field_limit) is not int or mismatch_field_limit <= 0):
        raise ValueError("mismatch_field_limit must be a positive integer or None")
    if type(confidence_gate) is not bool:
        raise ValueError("confidence_gate must be a boolean")
    if confidence_gate:
        if confidence_level is None:
            raise ValueError("confidence_gate requires an explicit confidence_level")
        for audit, rule in policy["metrics"].items():
            if rule["relative_tolerance"] != 0:
                raise ValueError(f"confidence_gate requires relative_tolerance == 0 for every metric; {audit} is not absolute-only")
    report: dict = {"schema_version": 1, "tool_version": __version__, "decision": "invalid",
                    "exit_code": 2, "policy": policy, "errors": [], "runs": {}, "groups": [], "mismatches": [],
                    "limitations": ["Lighthouse 12.8.2 navigation LHR projection only",
                                    "Heuristic raw MAD policy; no confidence intervals or significance claims",
                                    "Matching metadata does not guarantee matching hardware or site conditions"]}
    for side, directory in (("baseline", baseline_dir), ("current", current_dir)):
        directory = Path(directory)
        if side == "baseline" and directory.is_file():
            from .baseline import load_baseline
            runs = load_baseline(directory, policy)
            report["runs"][side] = runs
            report["baseline_manifest"] = {"schema_version": 1, "run_count": len(runs),
                                           "policy_matched": True}
            continue
        paths = []
        try:
            if not directory.is_dir():
                raise ValueError(f"{side} directory does not exist or is not a directory: {directory}")
            paths = sorted(directory.glob("*.json"))
            if not paths:
                raise ValueError(f"{side} directory contains no immediate *.json LHR files: {directory}")
        except (OSError, ValueError) as exc:
            report["errors"].append(str(exc))
        report["runs"][side] = [_read_run(path, policy["metrics"]) for path in paths]
    grouped: dict[str, dict] = {}
    for side, runs in report["runs"].items():
        for run in runs:
            report["errors"].extend(f"{side}/{run['file']}: {err}" for err in run["errors"])
            if run["group_id"]:
                group = grouped.setdefault(run["group_id"], {"id": run["group_id"], "identity": run["identity"],
                                                            "baseline": [], "current": []})
                group[side].append(run)
    report["mismatches"], mismatch_total, mismatch_groups = _profile_mismatches(grouped, mismatch_limit, mismatch_field_limit)
    if mismatch_limit is not None:
        emitted = len(report["mismatches"])
        report["mismatch_summary"] = {"limit": mismatch_limit, "total": mismatch_total, "emitted": emitted,
                                      "omitted": mismatch_total - emitted, "truncated": emitted < mismatch_total,
                                      "groups": mismatch_groups}
    for key in sorted(grouped):
        group = grouped[key]
        errors = []
        if not group["baseline"] or not group["current"]:
            errors.append("unmatched URL/version/config/browser group; no pooling or mismatch override is supported")
            report["errors"].append(f"group {key}: {errors[-1]}")
        metrics = [_metric_result(group["baseline"], group["current"], metric, rule, policy)
                   for metric, rule in sorted(policy["metrics"].items())]
        if errors:
            for metric in metrics:
                metric["decision"] = "invalid"
                metric["reasons"].extend(errors)
        decision = "invalid" if errors else aggregate([m["decision"] for m in metrics])
        report["groups"].append({"id": key, "identity": group["identity"], "errors": errors,
                                 "decision": decision, "metrics": metrics})
    report["decision"] = "invalid" if report["errors"] else aggregate([g["decision"] for g in report["groups"]])
    report["exit_code"] = EXIT_CODES[report["decision"]]
    if confidence_level is not None:
        add_confidence(report, confidence_level)
    if confidence_gate:
        apply_confidence_gate(report)
        for group in report["groups"]:
            group["decision"] = "invalid" if group["errors"] else aggregate([m["decision"] for m in group["metrics"]])
        report["decision"] = "invalid" if report["errors"] else aggregate([g["decision"] for g in report["groups"]])
        report["exit_code"] = EXIT_CODES[report["decision"]]
    return report
