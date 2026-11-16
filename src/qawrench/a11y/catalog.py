"""Pinned getRules exports and observed per-page rule coverage, not proof of fixes."""
from collections import Counter
from copy import deepcopy

from .core import BridgeError, VERSIONS, array, axe_target, canonical, impact, normalize_url, obj, text


def _metadata(rule, id_field):
    text(rule.get(id_field), f"catalog/result {id_field}")
    for field in ("description", "help"):
        text(rule.get(field), f"catalog/result {field}")
    normalize_url(rule.get("helpUrl"))
    for tag in array(rule.get("tags"), "catalog/result tags"):
        text(tag, "catalog/result tag")


def validate_catalog(document):
    """Validate the bridge envelope around an unfiltered axe.getRules() export.

    Version/ruleSet/export provenance is caller-attested; it is not authenticated.
    A catalog lists available rules, not necessarily enabled or executed rules.
    """
    obj(document, "rule catalog")
    if (set(document) != {"schemaVersion", "kind", "format", "inputVersion", "ruleSet", "export", "rules"}
            or type(document["schemaVersion"]) is not int or document["schemaVersion"] != 1
            or document["kind"] != "a11y-rule-catalog"):
        raise BridgeError("unsupported rule catalog schema or fields")
    if document["format"] != "axe" or document["inputVersion"] not in VERSIONS["axe"]:
        raise BridgeError("rule catalogs support only axe 4.10.3/4.11.0 getRules exports")
    text(document["ruleSet"], "catalog ruleSet")
    export = obj(document["export"], "catalog export")
    if set(export) != {"api", "tags"} or export["api"] != "axe.getRules" or export["tags"] is not None:
        raise BridgeError("catalog export must attest unfiltered axe.getRules with tags: null")
    ids = set()
    for rule in array(document["rules"], "catalog rules"):
        obj(rule, "catalog rule")
        required = {"ruleId", "description", "help", "helpUrl", "tags"}
        if not required <= set(rule) or set(rule) - required - {"actIds"}:
            raise BridgeError("unsupported getRules metadata fields in catalog")
        _metadata(rule, "ruleId")
        if "actIds" in rule:
            for act in array(rule["actIds"], "catalog actIds"):
                text(act, "catalog actId")
        if rule["ruleId"] in ids:
            raise BridgeError("duplicate rule ID in catalog")
        ids.add(rule["ruleId"])
    return deepcopy(document)


def catalog_coverage(document, snapshot, context):
    """Bind provenance and require every reported outcome to belong to the catalog."""
    catalog = validate_catalog(document)
    for field in ("format", "inputVersion", "ruleSet"):
        if catalog[field] != context[field]:
            raise BridgeError(f"catalog {field} does not match migration context")
    ids = {r["ruleId"] for r in catalog["rules"]}
    outcomes = {}
    report = snapshot["source"]["report"]
    # Incomplete results have already been rejected by snapshot validation.
    for group in ("violations", "passes", "inapplicable"):
        seen = set()
        for result in report[group]:
            obj(result, f"catalog coverage {group} result")
            _metadata(result, "id")
            rule = result["id"]
            if rule not in ids:
                raise BridgeError(f"reported rule {rule!r} is absent from its catalog")
            if rule in seen:
                raise BridgeError(f"duplicate rule ID in catalog coverage group {group}")
            seen.add(rule)
            impact(result.get("impact"), "catalog coverage impact")
            nodes = array(result.get("nodes"), "catalog coverage nodes")
            if (group == "inapplicable" and nodes) or (group != "inapplicable" and not nodes):
                raise BridgeError(f"invalid catalog coverage nodes for {group}")
            for node in nodes:
                obj(node, "catalog coverage node")
                axe_target(node.get("target"))
            outcomes.setdefault(rule, []).append(group)
    return {"catalog": catalog,
            "reportedRules": [{"rule": rule, "outcomes": sorted(outcomes[rule])} for rule in sorted(outcomes)],
            "notReportedRules": sorted(ids - outcomes.keys())}


def add_catalog_diagnostics(output, baseline, current, migration, baseline_catalog, current_catalog):
    """Enrich only explicit catalog mode; retain original occurrence classification."""
    if baseline_catalog is None and current_catalog is None:
        return
    if baseline_catalog is None or current_catalog is None:
        raise BridgeError("both baseline and current rule catalogs are required")
    if baseline["source"]["format"] != "axe":
        raise BridgeError("rule catalog coverage is supported only for axe reports")
    old = catalog_coverage(baseline_catalog, baseline, migration["baseline"])
    new = catalog_coverage(current_catalog, current, migration["current"])
    old_ids = {r["ruleId"] for r in old["catalog"]["rules"]}
    new_ids = {r["ruleId"] for r in new["catalog"]["rules"]}
    for rename in migration["renames"]:
        if rename["from"] not in old_ids or rename["to"] not in new_ids:
            raise BridgeError("migration rename endpoint is absent from its rule catalog")
    output["ruleCatalogs"] = {"baseline": old, "current": new,
                              "addedRules": sorted(new_ids - old_ids), "removedRules": sorted(old_ids - new_ids)}
    reported = {r["rule"]: r["outcomes"] for r in new["reportedRules"]}
    renames = {(r["engine"], r["from"]): r for r in migration["renames"]}
    # All native nodes have passed catalog validation. Count shared observations
    # from the full current report, including nodes already paired unchanged or renamed.
    target_nodes = Counter((result["id"], canonical(node["target"]), group)
                           for group in ("violations", "passes")
                           for result in current["source"]["report"][group]
                           for node in result["nodes"])
    for finding in output["removed"]:
        rule = finding["rule"]
        status = ("absent-from-current-catalog" if rule not in new_ids else
                  "reported-in-current" if rule in reported else "not-reported-in-current")
        finding["ruleCoverage"] = {"status": status, "outcomes": reported.get(rule, [])}
        counts = {group: target_nodes[rule, canonical(finding["target"]), group]
                  for group in ("violations", "passes")}
        finding["ruleTargetCoverage"] = {
            "rule": rule, "target": deepcopy(finding["target"]),
            "status": "observed-target-outcome" if any(counts.values()) else "no-observed-target-outcome",
            "observedNodes": counts,
        }
        rename = renames.get((finding["engine"], rule))
        if rename is not None:
            # Endpoints were validated above. This is destination-rule coverage
            # in the current page report, never evidence about the removed target.
            destination = rename["to"]
            finding["mappedRuleCoverage"] = {
                "rule": destination, "reason": rename["reason"],
                "status": "reported-in-current" if destination in reported else "not-reported-in-current",
                "outcomes": reported.get(destination, []),
            }
            counts = {group: target_nodes[destination, canonical(finding["target"]), group]
                      for group in ("violations", "passes")}
            finding["mappedTargetCoverage"] = {
                "rule": destination, "target": deepcopy(finding["target"]),
                "status": "observed-target-outcome" if any(counts.values()) else "no-observed-target-outcome",
                "observedNodes": counts,
            }
    baseline_reported = {r["rule"]: r["outcomes"] for r in old["reportedRules"]}
    # Observe the literal current ID in the full baseline report. Do not follow
    # aliases backwards or subtract nodes already paired unchanged.
    baseline_nodes = Counter((result["id"], canonical(node["target"]), group)
                             for group in ("violations", "passes")
                             for result in baseline["source"]["report"][group]
                             for node in result["nodes"])
    for finding in output["added"]:
        rule = finding["rule"]
        status = ("absent-from-baseline-catalog" if rule not in old_ids else
                  "reported-in-baseline" if rule in baseline_reported else "not-reported-in-baseline")
        finding["baselineRuleCoverage"] = {
            "status": status, "outcomes": list(baseline_reported.get(rule, [])),
        }
        counts = {group: baseline_nodes[rule, canonical(finding["target"]), group]
                  for group in ("violations", "passes")}
        finding["baselineRuleTargetCoverage"] = {
            "rule": rule, "target": deepcopy(finding["target"]),
            "status": "observed-target-outcome" if any(counts.values()) else "no-observed-target-outcome",
            "observedNodes": counts,
        }
