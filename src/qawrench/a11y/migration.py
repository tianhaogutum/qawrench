"""Explicit rule migration diagnostics; never a scan success or severity waiver."""
from copy import deepcopy

from .catalog import add_catalog_diagnostics

from .core import (BridgeError, POLICY, VERSIONS, _check_comparable, _pair_findings,
                   array, canonical, obj, text, validate_baseline)


def validate_migration(document):
    """Require a version-bound, one-to-one rename map with caller rule-set labels."""
    obj(document, "rule migration")
    if (set(document) != {"schemaVersion", "kind", "baseline", "current", "renames"}
            or type(document["schemaVersion"]) is not int or document["schemaVersion"] != 1
            or document["kind"] != "a11y-rule-migration"):
        raise BridgeError("unsupported rule migration schema or fields")
    for side in ("baseline", "current"):
        context = obj(document[side], f"rule migration.{side}")
        if set(context) != {"format", "inputVersion", "ruleSet"}:
            raise BridgeError("migration context requires exactly format/inputVersion/ruleSet")
        fmt = text(context["format"], "migration format")
        version = text(context["inputVersion"], "migration inputVersion")
        if fmt not in VERSIONS or version not in VERSIONS[fmt]:
            raise BridgeError("unsupported migration format/version")
        text(context["ruleSet"], "migration ruleSet")
    fmt = document["baseline"]["format"]
    if fmt != document["current"]["format"]:
        raise BridgeError("migration cannot cross report formats or engine namespaces")
    namespaces = ("axe",) if fmt == "axe" else ("pa11y-ci/htmlcs", "pa11y-ci/axe")
    renames, endpoints = [], set()
    for rename in array(document["renames"], "migration renames"):
        obj(rename, "migration rename")
        if set(rename) != {"engine", "from", "to", "reason"}:
            raise BridgeError("migration rename requires exactly engine/from/to/reason")
        engine = text(rename["engine"], "migration rename.engine")
        if engine not in namespaces:
            raise BridgeError("migration rename engine does not match report namespace")
        for field in ("from", "to", "reason"):
            text(rename[field], f"migration rename.{field}")
        old, new = (engine, rename["from"]), (engine, rename["to"])
        if old == new or old in endpoints or new in endpoints:
            raise BridgeError("ambiguous migration: renames must be one-to-one, without chains or reused endpoints")
        endpoints.update((old, new))
        renames.append(deepcopy(rename))
    return {**deepcopy(document), "renames": sorted(renames, key=canonical)}


def diagnose_migration(baseline, current, migration, *, baseline_catalog=None, current_catalog=None):
    """Classify occurrence changes without rewriting snapshots, gates or reviews.

    'removed' means absent from the validated current report, not a proven fix or
    proof that the scanner retired a rule. Rename semantics are caller-attested.
    """
    baseline, current = validate_baseline(baseline), validate_baseline(current)
    migration = validate_migration(migration)
    _check_comparable(baseline, current, allow_version_change=True)
    for side, snapshot in (("baseline", baseline), ("current", current)):
        for field in ("format", "inputVersion"):
            if migration[side][field] != snapshot["source"][field]:
                raise BridgeError(f"migration {side}.{field} does not match snapshot")
    old_rules = {(f["engine"], f["rule"]) for f in baseline["findings"]}
    new_rules = {(f["engine"], f["rule"]) for f in current["findings"]}
    for rename in migration["renames"]:
        if (rename["engine"], rename["to"]) in old_rules or (rename["engine"], rename["from"]) in new_rules:
            raise BridgeError("ambiguous migration: rename destination already existed or source still has violations")
    added, removed, unchanged = _pair_findings(baseline["findings"], current["findings"])
    renamed, audit = [], []
    for rename in migration["renames"]:
        before = [f for f in removed if (f["engine"], f["rule"]) == (rename["engine"], rename["from"])]
        after = [f for f in added if (f["engine"], f["rule"]) == (rename["engine"], rename["to"])]
        _, _, pairs = _pair_findings(before, after, key=lambda f: canonical([f["engine"], f["page"], f["target"]]))
        for pair in pairs:
            removed.remove(pair["baseline"])
            added.remove(pair["current"])
        renamed.extend(pairs)
        audit.append({**rename, "status": "matched" if pairs else "unmatched", "matchedOccurrences": len(pairs)})
    output = {
        "schemaVersion": 1, "kind": "a11y-migration-diagnostics", "identityPolicy": POLICY,
        "pages": current["pages"], "sources": {"baseline": baseline["source"], "current": current["source"]},
        "migration": {**migration, "renames": audit},
        "counts": {"baseline": len(baseline["findings"]), "current": len(current["findings"]),
                   "unchanged": len(unchanged), "renamed": len(renamed), "added": len(added), "removed": len(removed)},
        "unchanged": unchanged, "renamed": renamed, "added": added, "removed": removed,
    }
    add_catalog_diagnostics(output, baseline, current, migration, baseline_catalog, current_catalog)
    return output
