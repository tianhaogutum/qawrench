"""SARIF 2.1.0 output without fabricated source locations."""
from collections import defaultdict
from copy import deepcopy
import hashlib
import json
from urllib.parse import quote
from qawrench import __version__

from .core import canonical

HELP = {
    "axe": "https://github.com/dequelabs/axe-core/tree/v4.10.3/doc",
    "pa11y-ci/htmlcs": "https://github.com/squizlabs/HTML_CodeSniffer",
    "pa11y-ci/axe": "https://github.com/pa11y/pa11y/blob/9.0.0/lib/runners/axe.js",
}
LEVEL = {"minor": "note", "moderate": "warning", "serious": "error", "critical": "error", None: "error"}

MIGRATION_WARNING = (
    "Disappearances do not establish fixes or target retesting. "
    "Aliases are caller-declared; coverage counts are shared report observations, "
    "not occurrence-specific execution or remediation evidence."
)


def _page_uri(page):
    # Bridge-produced pages already have an ASCII/IDNA authority and a path.
    # Leave IPv6 brackets there intact, but escape brackets in path/query text.
    # Preserve percent escapes and delimiters verbatim; only the first # is a
    # fragment delimiter. This representation does not change native identity.
    scheme, scheme_sep, rest = page.partition("://")
    authority, path_sep, tail = rest.partition("/")
    path_query, fragment_sep, fragment = tail.partition("#")
    safe = "/?:@!$&'()*+,;=%"
    return (scheme + scheme_sep + authority + path_sep + quote(path_query, safe=safe)
            + fragment_sep + quote(fragment, safe=safe))


def _migration_message(description, baseline, current, finding, rename_reason=None):
    def literal(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    parts = [description + "."]
    for side, native in (("Baseline", baseline), ("Current", current)):
        if native is not None:
            parts.append(f'{side} identity: engine={literal(native["engine"])}, '
                         f'rule={literal(native["rule"])}, target={literal(native["target"])}, '
                         f'page={literal(native["page"])}.')
    if rename_reason is not None:
        parts.append(f'Caller-declared rename rationale: {literal(rename_reason)}.')
    if baseline is not None and current is None:
        mapped_reason = finding.get("mappedRuleCoverage", {}).get("reason")
        if mapped_reason is not None:
            parts.append(f'Caller-declared mapped-rule rationale: {literal(mapped_reason)}.')
    # Describe only annotations already present in the validated diagnostics.
    # Catalog availability and aliases alone never manufacture observations.
    for key, label, scope in (
        ("baselineRuleCoverage", "Baseline original-ID", "page"),
        ("baselineRuleTargetCoverage", "Baseline original-ID", "target"),
        ("ruleCoverage", "Current original-ID", "page"),
        ("ruleTargetCoverage", "Current original-ID", "target"),
        ("mappedRuleCoverage", "Current explicitly mapped", "page"),
        ("mappedTargetCoverage", "Current explicitly mapped", "target"),
    ):
        if key not in finding:
            continue
        coverage = finding[key]
        rule = coverage["rule"] if "rule" in coverage else finding["rule"]
        prefix = f'{label} {scope} observation: rule={literal(rule)}, '
        if scope == "page":
            parts.append(prefix + f'status={coverage["status"]}, outcomes={literal(coverage["outcomes"])}.')
        else:
            nodes = coverage["observedNodes"]
            parts.append(prefix + f'target={literal(coverage["target"])}, status={coverage["status"]}, '
                         f'observed nodes: violations={nodes["violations"]}, passes={nodes["passes"]}.')
    parts.append(MIGRATION_WARNING)
    return " ".join(parts)


def migration_to_sarif(diagnostics):
    """Export diagnose_migration output as notes, without baseline/fix semantics.

    Like to_sarif, this serializes bridge-produced diagnostics; it is not a
    validator for arbitrary caller-edited documents. Native identities and
    original/mapped coverage remain separate, verbatim properties.
    """
    descriptions = {
        "added": "Added migration observation",
        "removed": "Removed migration observation",
        "renamed": "Caller-declared rename observation",
    }
    rules = [{"id": f"a11y-migration/{status}", "shortDescription": {"text": description}}
             for status, description in sorted(descriptions.items())]
    indices = {rule["id"]: i for i, rule in enumerate(rules)}
    observations = [(status, finding) for status in descriptions for finding in diagnostics[status]]
    observations.sort(key=lambda item: (item[0], canonical(item[1])))
    rename_reasons = {(r["engine"], r["from"], r["to"]): r.get("reason")
                      for r in diagnostics["migration"]["renames"]}
    results, ordinal = [], defaultdict(int)
    for status, finding in observations:
        baseline = finding["baseline"] if status == "renamed" else finding if status == "removed" else None
        current = finding["current"] if status == "renamed" else finding if status == "added" else None
        rename_reason = (rename_reasons.get((baseline["engine"], baseline["rule"], current["rule"]))
                         if status == "renamed" and baseline["engine"] == current["engine"] else None)
        identity = [status, baseline["fingerprint"] if baseline else None,
                    current["fingerprint"] if current else None]
        observation_fp = hashlib.sha256(canonical(identity).encode("utf-8")).hexdigest()
        fingerprints = {"a11yMigrationObservation/v1": observation_fp}
        for side, native in (("Baseline", baseline), ("Current", current)):
            if native is not None:
                fingerprints[f"a11yMigration{side}Identity/v1"] = native["fingerprint"]
        ordinal[observation_fp] += 1
        rule_id = f"a11y-migration/{status}"
        page = (current if current is not None else baseline)["page"]
        results.append({
            "ruleId": rule_id, "ruleIndex": indices[rule_id], "level": "note",
            "message": {"text": _migration_message(descriptions[status], baseline, current, finding, rename_reason)},
            "partialFingerprints": fingerprints,
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": _page_uri(page)}}}],
            "properties": {**deepcopy(finding), "observation": status, "occurrence": ordinal[observation_fp]},
        })
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "a11y-baseline-bridge", "version": __version__, "rules": rules}},
            "results": results, "invocations": [{"executionSuccessful": True}],
            "properties": {**deepcopy({key: diagnostics[key] for key in (
                "kind", "counts", "pages", "sources", "migration", "identityPolicy", "ruleCatalogs"
            ) if key in diagnostics}), "interpretation": MIGRATION_WARNING},
        }],
    }


def to_sarif(delta):
    """Emit all occurrences, including fixed identities as baselineState=absent."""
    occurrences = [("new", f) for f in delta["new"]]
    occurrences += [("absent", f) for f in delta["fixed"]]
    occurrences += [("unchanged", pair["current"]) for pair in delta["unchanged"]]
    occurrences.sort(key=lambda item: (item[1]["engine"], item[1]["rule"], item[1]["fingerprint"], item[0], canonical(item[1])))
    rules = {}
    for _, f in occurrences:
        rule_id = f'{f["engine"]}:{f["rule"]}'
        if rule_id not in rules:
            rules[rule_id] = {
                "id": rule_id, "shortDescription": {"text": f["rule"]},
                "helpUri": _page_uri(f["helpUrl"] or HELP[f["engine"]]),
                "properties": {"engine": f["engine"], "nativeRuleId": f["rule"]},
            }
    ordered_rules = [rules[key] for key in sorted(rules)]
    indices = {rule["id"]: i for i, rule in enumerate(ordered_rules)}
    results, ordinal = [], defaultdict(int)
    for state, f in occurrences:
        rule_id = f'{f["engine"]}:{f["rule"]}'
        ordinal[f["fingerprint"]] += 1
        results.append({
            "ruleId": rule_id, "ruleIndex": indices[rule_id], "level": LEVEL[f["severity"]],
            "message": {"text": f["message"]}, "baselineState": state,
            "partialFingerprints": {"a11yBaselineIdentity/v1": f["fingerprint"]},
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": _page_uri(f["page"])}}}],
            "properties": {
                "engine": f["engine"], "nativeRuleId": f["rule"], "pageUrl": f["page"],
                "selector": f["target"], "impact": f["impact"], "severity": f["severity"],
                "occurrence": ordinal[f["fingerprint"]], "native": f["native"],
                **({"reviewedException": f["reviewedException"]} if "reviewedException" in f else {}),
            },
        })
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "a11y-baseline-bridge", "version": __version__, "rules": ordered_rules}},
            "results": results, "invocations": [{"executionSuccessful": True}],
            "properties": {"counts": delta["counts"], "gate": delta["gate"], "identityPolicy": delta["identityPolicy"],
                           **({"exceptions": delta["exceptions"]} if "exceptions" in delta else {})},
        }],
    }
