"""Validated adapters and deterministic multiset identity comparison."""
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import ipaddress
import json
import re
from urllib.parse import urlsplit, urlunsplit


class BridgeError(ValueError):
    """An unsupported, failed, incomplete or incomparable report."""


POLICY = "engine-page-rule-full-target/sha256-v1"
VERSIONS = {"axe": ("4.10.3", "4.11.0"), "pa11y-ci": ("4.0.0",)}
IMPACTS = ("minor", "moderate", "serious", "critical")
PA11Y_LEVELS = {"notice": "minor", "warning": "moderate", "error": "serious"}


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def obj(value, where):
    if not isinstance(value, dict):
        raise BridgeError(f"{where}: expected an object")
    return value


def array(value, where):
    if not isinstance(value, list):
        raise BridgeError(f"{where}: expected an array")
    return value


def text(value, where, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise BridgeError(f"{where}: expected {'a' if empty else 'a nonempty'} string")
    return value


def normalize_url(value):
    """Conservative HTTP(S) normalization; preserve query ordering and fragments."""
    text(value, "page URL")
    if re.search(r"[\s\x00-\x1f\x7f]", value) or re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise BridgeError("page URL: whitespace, controls or invalid percent escape")
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            raise ValueError("absolute http(s) URL required")
        if parts.username is not None or parts.password is not None or "\\" in value:
            raise ValueError("credentials and backslashes are unsupported")
        host = parts.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = f"[{ipaddress.IPv6Address(host).compressed}]"
        elif not re.fullmatch(r"[a-z0-9.-]+", host):
            raise ValueError("unsupported host")
        port = parts.port
        default = 443 if parts.scheme.lower() == "https" else 80
        netloc = host if port is None or port == default else f"{host}:{port}"
        return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, parts.fragment))
    except (ValueError, UnicodeError) as exc:
        raise BridgeError(f"page URL: {exc}") from exc


def axe_target(value):
    array(value, "axe node.target")
    if not value:
        raise BridgeError("axe node.target: empty targets are unsupported; enable selectors")
    for part in value:
        if isinstance(part, list):
            if len(part) < 2:
                raise BridgeError("axe shadow target: expected at least two selector strings")
            for selector in part:
                text(selector, "axe shadow target selector")
        else:
            text(part, "axe frame target selector")
    return deepcopy(value)


def impact(value, where):
    if value is not None and value not in IMPACTS:
        raise BridgeError(f"{where}: unknown impact {value!r}")
    return value


def fingerprint(engine, page, rule, target):
    payload = canonical([engine, normalize_url(page), rule, target])
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def finding(engine, page, rule, target, message, severity, help_url, native_impact, native):
    return {
        "engine": engine, "page": page, "rule": rule, "target": target,
        "message": message, "severity": severity, "impact": native_impact,
        "helpUrl": help_url,
        "fingerprint": fingerprint(engine, page, rule, target), "native": deepcopy(native),
    }


def reject_failure(report):
    for key in ("error", "exception", "scanError"):
        if report.get(key) is not None:
            raise BridgeError(f"scan failure: {key} is present")
    if report.get("complete", True) is not True or report.get("status", "complete") != "complete":
        raise BridgeError("incomplete/failed scan status")


def adapt_axe(report, fallback_url):
    engine = obj(report.get("testEngine"), "axe testEngine")
    if engine.get("name") != "axe-core" or engine.get("version") not in VERSIONS["axe"]:
        raise BridgeError("unknown axe engine/version; supported: axe-core 4.10.3, 4.11.0")
    if report.get("errors"):
        raise BridgeError("axe scan errors are present")
    page = normalize_url(report.get("url") if report.get("url") is not None else fallback_url)
    for name in ("violations", "incomplete", "passes", "inapplicable"):
        array(report.get(name), f"axe {name} (full results object required)")
    if report["incomplete"]:
        raise BridgeError("axe incomplete contains review/aborted results; cannot count as clean")
    options = obj(report.get("toolOptions"), "axe toolOptions")
    if options.get("reporter", "v1") != "v1":
        raise BridgeError("unsupported axe reporter; only the full v1 results format is supported")
    if options.get("selectors", True) is not True:
        raise BridgeError("axe toolOptions.selectors must be true")
    if "resultTypes" in options:
        types = array(options["resultTypes"], "axe toolOptions.resultTypes")
        if "violations" not in types:
            raise BridgeError("axe resultTypes must include violations (otherwise nodes may be truncated)")
    findings = []
    for ri, rule in enumerate(report["violations"]):
        obj(rule, f"axe violations[{ri}]")
        rule_id = text(rule.get("id"), "axe rule.id")
        help_text = text(rule.get("help"), "axe rule.help")
        help_url = normalize_url(rule.get("helpUrl"))
        rule_impact = impact(rule.get("impact"), "axe rule.impact")
        nodes = array(rule.get("nodes"), "axe rule.nodes")
        if not nodes:
            raise BridgeError("axe violation has no nodes; truncated or malformed results")
        for node in nodes:
            obj(node, "axe node")
            target = axe_target(node.get("target"))
            node_impact = impact(node.get("impact", rule_impact), "axe node.impact")
            message = text(node.get("failureSummary", help_text), "axe node.failureSummary")
            native = {"rule": {k: v for k, v in rule.items() if k != "nodes"}, "node": node}
            findings.append(finding("axe", page, rule_id, target, message, node_impact,
                                    help_url, node_impact, native))
    return [page], findings


def adapt_pa11y(report, fallback_url):
    if fallback_url is not None:
        raise BridgeError("--url is only for axe; pa11y-ci pages come from results keys")
    results = obj(report.get("results"), "pa11y-ci results (combined report object required)")
    counts = {}
    for name in ("total", "passes", "errors"):
        value = report.get(name)
        if type(value) is not int or value < 0:
            raise BridgeError(f"pa11y-ci {name}: expected nonnegative integer")
        counts[name] = value
    if not results or counts["total"] != len(results):
        raise BridgeError("pa11y-ci incomplete run: total must equal nonempty results page count; duplicate URLs unsupported")
    findings, pages = [], []
    issue_count = clean_pages = 0
    for raw_page, issues in results.items():
        page = normalize_url(raw_page)
        if page in pages:
            raise BridgeError("pa11y-ci page keys collide after URL normalization")
        pages.append(page)
        array(issues, f"pa11y-ci results[{raw_page!r}]")
        clean_pages += not issues
        issue_count += len(issues)
        for issue in issues:
            obj(issue, "pa11y-ci issue (scan failures are not issues)")
            # Error objects often serialize as {}; never accept these as a clean scan.
            required = ("code", "message", "selector", "type", "typeCode", "runner", "runnerExtras")
            if any(key not in issue for key in required):
                raise BridgeError("pa11y-ci malformed issue or scan failure: expected code/message/selector/type/typeCode/runner/runnerExtras")
            runner = issue["runner"]
            if runner not in ("htmlcs", "axe"):
                raise BridgeError(f"unknown pa11y-ci runner {runner!r}; supported: htmlcs, axe")
            native_type = text(issue["type"], "pa11y-ci type")
            if native_type not in PA11Y_LEVELS:
                raise BridgeError(f"pa11y-ci unknown issue type {native_type!r}")
            if type(issue["typeCode"]) is not int or issue["typeCode"] != {"error": 1, "warning": 2, "notice": 3}[native_type]:
                raise BridgeError("pa11y-ci typeCode contradicts type")
            extras = obj(issue["runnerExtras"], "pa11y-ci runnerExtras")
            native_impact = impact(extras.get("impact"), "pa11y-ci runnerExtras.impact")
            help_url = normalize_url(extras["helpUrl"]) if "helpUrl" in extras else None
            findings.append(finding(
                f"pa11y-ci/{runner}", page, text(issue["code"], "pa11y-ci code"),
                text(issue["selector"], "pa11y-ci selector", empty=True),
                text(issue["message"], "pa11y-ci message"),
                native_impact or PA11Y_LEVELS[native_type], help_url, native_impact, issue,
            ))
    if issue_count != counts["errors"] or clean_pages != counts["passes"]:
        raise BridgeError("pa11y-ci counters contradict results; incomplete run or scan failure")
    return sorted(pages), findings


def make_baseline(report, input_format, *, url=None, input_version=None):
    """Adapt a full native report into a versioned, self-validating snapshot."""
    obj(report, "report")
    try:
        canonical(report)
    except (TypeError, ValueError, RecursionError) as exc:
        raise BridgeError(f"report must contain finite, serializable JSON data: {exc}") from exc
    text(input_format, "input format")
    if input_format not in VERSIONS:
        raise BridgeError(f"unknown format {input_format!r}; supported: axe, pa11y-ci")
    if input_format == "pa11y-ci" and input_version is None:
        raise BridgeError("pa11y-ci report has no version: supply --input-version 4.0.0")
    embedded = obj(report.get("testEngine"), "axe testEngine").get("version") if input_format == "axe" else None
    version = input_version if input_version is not None else embedded
    if version not in VERSIONS[input_format] or ("version" in report and report["version"] != version):
        label = "engine/version" if input_format == "axe" else "version"
        raise BridgeError(f"unknown {input_format} {label}; supported: {', '.join(VERSIONS[input_format])}")
    if embedded is not None and embedded != version:
        raise BridgeError("axe input version contradicts embedded testEngine.version")
    reject_failure(report)
    pages, findings = (adapt_axe if input_format == "axe" else adapt_pa11y)(report, url)
    return {
        "schemaVersion": 1, "kind": "a11y-baseline", "identityPolicy": POLICY,
        "source": {"format": input_format, "inputVersion": version, "fallbackUrl": url,
                   "report": deepcopy(report)},
        "pages": pages, "findings": sorted(findings, key=canonical),
    }


def validate_baseline(baseline):
    obj(baseline, "baseline")
    if baseline.get("schemaVersion") != 1 or baseline.get("kind") != "a11y-baseline" or baseline.get("identityPolicy") != POLICY:
        raise BridgeError("unsupported baseline schema or identity policy")
    source = obj(baseline.get("source"), "baseline.source")
    rebuilt = make_baseline(source.get("report"), source.get("format"),
                            url=source.get("fallbackUrl"), input_version=source.get("inputVersion"))
    if canonical(baseline) != canonical(rebuilt):
        raise BridgeError("baseline contents/fingerprints do not match its native source; regenerate baseline")
    return rebuilt


def validate_exceptions(document):
    """Validate an explicit review file; identities never contain patterns."""
    obj(document, "reviewed exceptions")
    if (set(document) != {"schemaVersion", "kind", "identityPolicy", "exceptions"}
            or type(document.get("schemaVersion")) is not int or document["schemaVersion"] != 1
            or document.get("kind") != "a11y-reviewed-exceptions" or document.get("identityPolicy") != POLICY):
        raise BridgeError("unsupported reviewed exceptions schema, fields or identity policy")
    entries, seen = [], set()
    for entry in array(document["exceptions"], "reviewed exceptions.exceptions"):
        obj(entry, "reviewed exception")
        if set(entry) != {"engine", "page", "rule", "target", "owner", "reason", "expires", "maxOccurrences"}:
            raise BridgeError("reviewed exception requires exactly engine/page/rule/target/owner/reason/expires/maxOccurrences")
        engine = text(entry["engine"], "reviewed exception.engine")
        if engine not in ("axe", "pa11y-ci/htmlcs", "pa11y-ci/axe"):
            raise BridgeError("reviewed exception.engine: unsupported namespace")
        page = normalize_url(entry["page"])
        rule = text(entry["rule"], "reviewed exception.rule")
        target = (axe_target(entry["target"]) if engine == "axe"
                  else text(entry["target"], "reviewed exception.target", empty=True))
        for field in ("owner", "reason"):
            text(entry[field], f"reviewed exception.{field}")
        expires = text(entry["expires"], "reviewed exception.expires")
        try:
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", expires):
                raise ValueError("expected YYYY-MM-DD")
            date.fromisoformat(expires)
        except ValueError as exc:
            raise BridgeError("reviewed exception.expires: expected a valid YYYY-MM-DD date") from exc
        if type(entry["maxOccurrences"]) is not int or entry["maxOccurrences"] < 1:
            raise BridgeError("reviewed exception.maxOccurrences: expected a positive integer")
        identity = fingerprint(engine, page, rule, target)
        if identity in seen:
            raise BridgeError("duplicate reviewed exception identity (after URL normalization)")
        seen.add(identity)
        entries.append({**deepcopy(entry), "page": page, "target": target})
    return {**document, "exceptions": sorted(entries, key=canonical)}


def _apply_exceptions(document, added, unchanged, evaluation_date):
    document = validate_exceptions(document)
    if evaluation_date is None:
        evaluation_date = datetime.now(timezone.utc).date()
    if type(evaluation_date) is not date:
        raise BridgeError("exception evaluation_date must be a datetime.date")
    new_by_identity, unchanged_counts = defaultdict(list), defaultdict(int)
    for f in added:
        new_by_identity[f["fingerprint"]].append(f)
    for pair in unchanged:
        unchanged_counts[pair["current"]["fingerprint"]] += 1
    entries = []
    counts = {"active": 0, "expired": 0, "unmatched": 0, "exceptedNew": 0}
    for entry in document["exceptions"]:
        identity = fingerprint(entry["engine"], entry["page"], entry["rule"], entry["target"])
        existing = unchanged_counts[identity]
        new = new_by_identity[identity]
        current_count = existing + len(new)
        status = ("expired" if date.fromisoformat(entry["expires"]) <= evaluation_date
                  else "unmatched" if current_count == 0 else "active")
        # Review a total current-occurrence budget, never an unlimited identity.
        allowance = max(0, entry["maxOccurrences"] - existing) if status == "active" else 0
        excepted = new[:allowance]
        for f in excepted:
            f["reviewedException"] = {"fingerprint": identity, **deepcopy(entry)}
        entries.append({**entry, "fingerprint": identity, "status": status,
                        "currentOccurrences": current_count, "unchangedOccurrences": existing,
                        "exceptedNewOccurrences": len(excepted)})
        counts[status] += 1
        counts["exceptedNew"] += len(excepted)
    return {"evaluatedOn": evaluation_date.isoformat(), "entries": entries, "counts": counts}


def _check_comparable(baseline, current, *, allow_version_change=False):
    for key in (("format",) if allow_version_change else ("format", "inputVersion")):
        if baseline["source"][key] != current["source"][key]:
            raise BridgeError(f"incomparable reports: {key} changed")
    if baseline["pages"] != current["pages"]:
        raise BridgeError("incomparable reports: page coverage changed (missing/additional page)")
    if baseline["source"]["format"] == "axe":
        if canonical(baseline["source"]["report"]["toolOptions"]) != canonical(current["source"]["report"]["toolOptions"]):
            raise BridgeError("incomparable axe reports: toolOptions changed")


def _pair_findings(baseline_findings, current_findings, *, key=lambda f: f["fingerprint"]):
    old, new = defaultdict(list), defaultdict(list)
    for f in baseline_findings:
        old[key(f)].append(f)
    for f in current_findings:
        new[key(f)].append(f)
    added, fixed, unchanged = [], [], []
    for identity in sorted(old.keys() | new.keys()):
        before, after = old[identity], new[identity]
        # Pair exact metadata matches first, then canonically; report order never matters.
        for f in before[:]:
            if f in after:
                unchanged.append({"baseline": f, "current": after.pop(after.index(f))})
                before.remove(f)
        shared = min(len(before), len(after))
        unchanged.extend({"baseline": b, "current": c} for b, c in zip(before[:shared], after[:shared]))
        fixed.extend(before[shared:])
        added.extend(after[shared:])
    return added, fixed, unchanged


def compare(baseline, current, *, fail_on="none", exceptions=None, evaluation_date=None):
    """Compare snapshots by multiset identity; unchanged retains both occurrences."""
    baseline, current = validate_baseline(baseline), validate_baseline(current)
    if fail_on not in ("none", *IMPACTS):
        raise BridgeError(f"unknown severity gate {fail_on!r}")
    _check_comparable(baseline, current)
    added, fixed, unchanged = _pair_findings(baseline["findings"], current["findings"])
    review_audit = _apply_exceptions(exceptions, added, unchanged, evaluation_date) if exceptions is not None else None
    gated = [] if fail_on == "none" else [f for f in added if f["severity"] is None or IMPACTS.index(f["severity"]) >= IMPACTS.index(fail_on)]
    gate = {"failOn": fail_on, "passed": not gated, "newAtOrAboveThreshold": len(gated)}
    if review_audit is not None:
        excepted = sum("reviewedException" in f for f in gated)
        gate.update(passed=len(gated) == excepted, exceptedNewAtOrAboveThreshold=excepted,
                    unexceptedNewAtOrAboveThreshold=len(gated) - excepted)
    return {
        "schemaVersion": 1, "kind": "a11y-delta", "identityPolicy": POLICY,
        "pages": current["pages"],
        "sources": {"baseline": baseline["source"], "current": current["source"]},
        "counts": {"new": len(added), "fixed": len(fixed), "unchanged": len(unchanged),
                   "baseline": len(baseline["findings"]), "current": len(current["findings"])},
        "new": added, "fixed": fixed, "unchanged": unchanged,
        "gate": gate,
        **({"exceptions": review_audit} if review_audit is not None else {}),
    }
