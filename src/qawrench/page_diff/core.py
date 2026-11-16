"""Own comparison implementation; no changedetection.io code is imported."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag
import soupsieve


class ContractError(ValueError):
    """The supplied input cannot satisfy the explicit comparison contract."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON property: {key}")
        result[key] = value
    return result


def read_utf8(path: str | Path) -> str:
    try:
        return Path(path).read_bytes().decode("utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise ContractError(f"cannot read UTF-8 file {path}: {exc}") from exc


def load_rules(path: str | Path) -> dict:
    try:
        return validate_rules(json.loads(read_utf8(path), object_pairs_hook=_object))
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid rules JSON: {exc}") from exc


def _fields(obj, allowed, required, where):
    if not isinstance(obj, dict):
        raise ContractError(f"{where} must be an object")
    if set(obj) - allowed or required - set(obj):
        raise ContractError(f"{where}: unknown or missing fields; allowed={sorted(allowed)}, required={sorted(required)}")


def _selector(value, where):
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{where} must be a nonempty CSS selector")
    try:
        soupsieve.compile(value)
    except (soupsieve.SelectorSyntaxError, NotImplementedError) as exc:
        raise ContractError(f"{where}: invalid/unsupported selector {value!r}: {exc}") from exc


def _rule_entries(rules, field):
    for index, entry in enumerate(rules.get(field, [])):
        if field == "ignore_selectors" and isinstance(entry, str):
            entry = {"selector": entry}
        yield f"{field}[{index}]", entry


def _metadata(entry, where):
    if "min_matches" in entry and (type(entry["min_matches"]) is not int or entry["min_matches"] < 0):
        raise ContractError(f"{where}.min_matches must be a nonnegative integer")
    if "max_matches" in entry:
        if type(entry["max_matches"]) is not int or entry["max_matches"] < 0:
            raise ContractError(f"{where}.max_matches must be a nonnegative integer")
        if entry["max_matches"] < entry.get("min_matches", 1):
            raise ContractError(f"{where}.max_matches must be >= min_matches (default 1)")
    if "owner" in entry and (not isinstance(entry["owner"], str) or not entry["owner"].strip()):
        raise ContractError(f"{where}.owner must be a nonempty string")
    if "expires" in entry:
        value = entry["expires"]
        try:
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError as exc:
            raise ContractError(f"{where}.expires must be a valid YYYY-MM-DD date") from exc


def _utc_today():
    return datetime.now(timezone.utc).date()


def _check_expiry(rules, today):
    for field in ("ignore_selectors", "volatile_attributes", "unordered_collections"):
        for rule_id, entry in _rule_entries(rules, field):
            if "expires" in entry and today >= date.fromisoformat(entry["expires"]):
                raise ContractError(f"rule expired: {rule_id} selector={entry['selector']!r} "
                                    f"owner={entry.get('owner', '(unspecified)')!r} "
                                    f"expires={entry['expires']} evaluated_on={today.isoformat()}")


def validate_rules(rules: dict) -> dict:
    allowed = {"version", "required_region", "required_selectors", "required_attributes", "ignore_selectors", "normalize_whitespace", "volatile_attributes", "unordered_collections", "sibling_identity"}
    _fields(rules, allowed, {"version", "required_region"}, "rules")
    if type(rules["version"]) is not int or rules["version"] != 1:
        raise ContractError("rules.version must be integer 1")
    _selector(rules["required_region"], "required_region")
    result = {"normalize_whitespace": False, "required_selectors": [], "ignore_selectors": [], "volatile_attributes": [], "unordered_collections": [], "sibling_identity": [], **rules}
    if type(result["normalize_whitespace"]) is not bool:
        raise ContractError("normalize_whitespace must be boolean")
    for field in ("required_selectors", "required_attributes", "ignore_selectors", "volatile_attributes", "unordered_collections", "sibling_identity"):
        if field in result and not isinstance(result[field], list):
            raise ContractError(f"{field} must be a list")
    for selector in result["required_selectors"]:
        _selector(selector, "required_selectors")
    # Keep this optional field absent when omitted, preserving legacy reports.
    for rule_id, entry in _rule_entries(result, "required_attributes"):
        _fields(entry, {"selector", "attributes"}, {"selector", "attributes"}, rule_id)
        _selector(entry["selector"], rule_id + ".selector")
        attrs = entry["attributes"]
        if not isinstance(attrs, list) or not attrs or any(not isinstance(a, str) or not re.fullmatch(r"[a-z_:][a-z0-9_.:-]*", a) for a in attrs) or len(set(attrs)) != len(attrs):
            raise ContractError(f"{rule_id}.attributes must be a nonempty unique list of lowercase HTML attribute names")
    for rule_id, entry in _rule_entries(result, "ignore_selectors"):
        _fields(entry, {"selector", "min_matches", "max_matches", "owner", "expires"}, {"selector"}, rule_id)
        _selector(entry["selector"], rule_id + ".selector")
        _metadata(entry, rule_id)
    for rule_id, entry in _rule_entries(result, "volatile_attributes"):
        _fields(entry, {"selector", "attributes", "min_matches", "max_matches", "owner", "expires"}, {"selector", "attributes"}, rule_id)
        _selector(entry["selector"], rule_id + ".selector")
        _metadata(entry, rule_id)
        attrs = entry["attributes"]
        if not isinstance(attrs, list) or not attrs or any(not isinstance(a, str) or not re.fullmatch(r"[a-z_:][a-z0-9_.:-]*", a) for a in attrs) or len(set(attrs)) != len(attrs):
            raise ContractError("attributes must be a nonempty unique list of lowercase HTML attribute names")
    seen = set()
    for rule_id, entry in _rule_entries(result, "unordered_collections"):
        _fields(entry, {"selector", "items", "key_attribute", "owner", "expires"}, {"selector", "items", "key_attribute"}, rule_id)
        _selector(entry["selector"], "collection.selector")
        _selector(entry["items"], "collection.items")
        _metadata(entry, rule_id)
        if not isinstance(entry["key_attribute"], str) or not re.fullmatch(r"[a-z_:][a-z0-9_.:-]*", entry["key_attribute"]):
            raise ContractError("key_attribute must be a lowercase HTML attribute name")
        if entry["selector"] in seen:
            raise ContractError("duplicate collection selector")
        seen.add(entry["selector"])
    for rule_id, entry in _rule_entries(result, "sibling_identity"):
        _fields(entry, {"selector", "key_attribute"}, {"selector", "key_attribute"}, rule_id)
        _selector(entry["selector"], rule_id + ".selector")
        if not isinstance(entry["key_attribute"], str) or not re.fullmatch(r"[a-z_:][a-z0-9_.:-]*", entry["key_attribute"]):
            raise ContractError(f"{rule_id}.key_attribute must be a lowercase HTML attribute name")
    return result


def _select(root, selector):
    # Soup.select does not include root; matching root is useful for #catalog.
    return ([root] if isinstance(root, Tag) and root.name != "[document]" and soupsieve.match(selector, root) else []) + list(root.select(selector))


def _has_text(node):
    return any(type(s) is NavigableString and str(s).strip() for s in node.descendants)


def _original_element_path(node, root):
    # Parsed text gates precede collection/key validation and the full locator.
    # Locate only the failing element, without moving any validation earlier.
    parts = []
    while node is not root:
        index = sum(isinstance(sibling, Tag) and sibling.name == node.name
                    for sibling in node.previous_siblings)
        parts.append(_segment(f"{node.name}[{index}]"))
        node = node.parent
    return "/region" + "".join("/" + part for part in reversed(parts))


def _required(root, rules, side, stage, original_paths=None):
    if not _has_text(root):
        raise ContractError(f"{side}: required region has no text content "
                            f"stage={stage} original_path='/region'")
    observed = []
    for index, selector in enumerate(rules["required_selectors"]):
        matches = _select(root, selector)
        prefix = (f"{side}: required text contract failed: required_selectors[{index}] "
                  f"selector={selector!r} stage={stage}")
        if not matches:
            raise ContractError(f"{prefix} expected at least 1 matched elements, got 0 "
                                "original_path=null original_scope_path='/region'")
        for node in matches:
            if not _has_text(node):
                # Later stages must retain indices of siblings already ignored.
                path = (original_paths[id(node)] if original_paths is not None else
                        _original_element_path(node, root))
                raise ContractError(f"{prefix} empty text original_path={path!r}")
        observed.append((selector, matches))
    return observed


def _segment(value):
    return str(value).replace("~", "~0").replace("/", "~1")


def _prepare(html, rules, side):
    if not isinstance(html, str) or not html.strip() or "\x00" in html:
        raise ContractError(f"{side}: HTML must be nonempty text without NUL characters")
    def duplicate_attribute(attrs, name, value):
        raise ContractError(f"{side}: duplicate HTML attribute {name!r}; ambiguous input is unsupported")

    # html.parser's default BeautifulSoup builder collapses whitespace-only
    # strings. Preserve them under the document root so only our explicit rule
    # can suppress whitespace changes. Attribute token lists also stay strings.
    soup = BeautifulSoup(html, "html.parser", multi_valued_attributes=None,
                         preserve_whitespace_tags={"[document]"},
                         on_duplicate_attribute=duplicate_attribute)
    roots = soup.select(rules["required_region"])
    if len(roots) != 1:
        raise ContractError(f"{side}: required_region must match exactly one element, got {len(roots)}")
    root = roots[0]
    parsed_required = _required(root, rules, side, "parsed")
    coverage = {}

    def observe(rule_id, entry, matches, stage, **details):
        minimum = entry.get("min_matches", 1)
        bound = None
        if len(matches) < minimum:
            bound = ("least", minimum)
        elif "max_matches" in entry and len(matches) > entry["max_matches"]:
            bound = ("most", entry["max_matches"])
        if bound is not None:
            # Configurable bounds belong to ignore/volatile rules. Reuse their
            # actual selection inventory, already located before any removals.
            location = ("original_paths=" + json.dumps([
                node["original_path"] for node in details["matched_nodes"]]) if matches else
                "original_path=null original_scope_path='/region'")
            raise ContractError(f"{side}: normalization coverage failed: {rule_id} "
                                f"selector={entry['selector']!r} stage={stage} expected at {bound[0]} {bound[1]} "
                                f"matched elements, got {len(matches)} "
                                f"(owner={entry.get('owner', '(unspecified)')!r}) {location}")
        coverage[rule_id] = {"rule_id": rule_id, "selector": entry["selector"],
                             "matched_elements": len(matches), "min_matches": minimum,
                             **{key: entry[key] for key in ("max_matches", "owner", "expires") if key in entry}, **details}

    collections, keyed = {}, {}
    for rule_id, rule in _rule_entries(rules, "unordered_collections"):
        matches = _select(root, rule["selector"])
        if len(matches) != 1:
            raise ContractError(f"{side}: {rule_id} collection {rule['selector']!r} must match exactly once, "
                                f"got {len(matches)} (owner={rule.get('owner', '(unspecified)')!r})")
        container = matches[0]
        if id(container) in collections:
            raise ContractError(f"{side}: overlapping collection selectors")
        items = list(container.select(rule["items"]))
        if any(item.parent is not container for item in items):
            raise ContractError(f"{side}: collection items must be direct children")
        item_ids = {id(item) for item in items}
        for child in container.children:
            if id(child) in item_ids or (type(child) is NavigableString and not str(child).strip()):
                continue
            raise ContractError(f"{side}: collection contains non-item content; put headings/other content outside its container")
        keys = []
        for item in items:
            key = item.get(rule["key_attribute"])
            if not isinstance(key, str) or not key.strip():
                raise ContractError(f"{side}: missing/empty item key {rule['key_attribute']}")
            if key in keys:
                raise ContractError(f"{side}: duplicate item key {key!r} in {rule['selector']}")
            keys.append(key)
            keyed[id(item)] = (rule["key_attribute"], key)
        collections[id(container)] = (rule_id, rule, items, keys)
        observe(rule_id, rule, matches, "parsed", item_count=len(items))
    for cid in collections:
        container = next(n for n in [root, *root.descendants] if id(n) == cid)
        if any(id(a) in collections for a in container.parents):
            raise ContractError(f"{side}: nested unordered collections are unsupported")
    identities, identity_counts = {}, defaultdict(int)
    for rule_id, rule in _rule_entries(rules, "sibling_identity"):
        matches = _select(root, rule["selector"])
        coverage[rule_id] = {"rule_id": rule_id, "selector": rule["selector"],
                             "matched_elements": len(matches), "key_attribute": rule["key_attribute"],
                             "matched_nodes": []}
        for node in matches:
            if node is root or id(node) in keyed or id(node) in identities:
                raise ContractError(f"{side}: {rule_id} targets region root, unordered item, or overlapping identity rule")
            attr = rule["key_attribute"]
            key = node.get(attr)
            key = key if isinstance(key, str) and key.strip() else None
            identities[id(node)] = (rule_id, attr, key)
            identity_counts[(id(node.parent), rule_id, key)] += 1
    paths, original_paths, ambiguous_paths = {}, {}, {}

    def locate(node, path, original_path, ambiguous=None):
        paths[id(node)] = path
        original_paths[id(node)] = original_path
        if id(node) in identities:
            rule_id, _, key = identities[id(node)]
            # Snapshot-local candidates in original DOM preorder, not pairings.
            coverage[rule_id]["matched_nodes"].append({
                "original_path": original_path,
                "original_parent_path": original_paths[id(node.parent)], "key": key})
            if key is None or identity_counts[(id(node.parent), rule_id, key)] > 1:
                ambiguous = ambiguous or path
        if ambiguous is not None:
            ambiguous_paths[id(node)] = ambiguous
        if not isinstance(node, Tag):
            return
        counters = defaultdict(int)
        for child in node.children:
            kind = child.name if isinstance(child, Tag) else ("#text" if type(child) is NavigableString else "#" + type(child).__name__)
            index = counters[kind]
            counters[kind] += 1
            if id(child) in keyed:
                attr, key = keyed[id(child)]
                part = f"item[{attr}={json.dumps(key, ensure_ascii=False)}]"
            elif id(child) in identities:
                rule_id, attr, key = identities[id(child)]
                part = _identity_segment(rule_id, attr, key)
            else:
                part = f"{kind}[{index}]"
            locate(child, path + "/" + _segment(part), original_path + "/" + _segment(f"{kind}[{index}]"), ambiguous)

    locate(root, "/region", "/region")

    def required_text_coverage(observed, stage):
        # Observe the gate's actual selections, independently at each stage.
        # Original paths precede all removals and keyed collection sorting.
        for index, (selector, matches) in enumerate(observed):
            rule_id = f"required_selectors[{index}]"
            located = {"matched_elements": len(matches), "matched_nodes": [
                {"original_path": original_paths[id(node)]} for node in matches]}
            if stage == "parsed":
                coverage[rule_id] = {"rule_id": rule_id, "selector": selector, **located}
            else:
                coverage[rule_id][stage] = located

    required_text_coverage(parsed_required, "parsed")
    # Snapshot-local collection evidence in original item order, before mutation.
    for cid, (rule_id, _, items, keys) in collections.items():
        coverage[rule_id].update(original_path=original_paths[cid], matched_items=[
            {"original_path": original_paths[id(item)],
             "original_parent_path": original_paths[cid], "key": key}
            for item, key in zip(items, keys)])
    required_nodes = {}

    def required_attributes(stage):
        if not rules.get("required_attributes"):
            return
        present = {id(root), *(id(n) for n in root.descendants)}
        for rule_id, rule in _rule_entries(rules, "required_attributes"):
            matches = _select(root, rule["selector"])
            # Retain earlier obligations even if CSS membership changes. New
            # matches at later stages are independent obligations from then on.
            obligated = required_nodes.setdefault(rule_id, {})
            obligated.update((id(node), node) for node in matches)
            prefix = (f"{side}: required attribute contract failed: {rule_id} "
                      f"selector={rule['selector']!r} stage={stage}")
            for node_id, node in obligated.items():
                for attr in rule["attributes"]:
                    reason = ("removed element" if node_id not in present else
                              "missing" if attr not in node.attrs else
                              "blank" if not node.attrs[attr].strip() else None)
                    if reason:
                        raise ContractError(f"{prefix} attribute={attr!r} {reason} "
                                            f"original_path={original_paths[node_id]!r}")
            if not matches:
                raise ContractError(f"{prefix} attributes={rule['attributes']!r} expected at least 1 "
                                    "matched elements, got 0 original_path=null original_scope_path='/region'")
            observed = {"matched_elements": len(matches), "matched_nodes": [
                {"original_path": original_paths[id(node)]} for node in matches]}
            if stage == "parsed":
                coverage[rule_id] = {"rule_id": rule_id, "selector": rule["selector"],
                                     "attributes": list(rule["attributes"]), **observed}
            else:
                coverage[rule_id][stage] = observed

    required_attributes("parsed")
    events = []

    def event(rule, node, old, new, suffix="", **details):
        details["original_path"] = original_paths[id(node)] + suffix
        if rules["sibling_identity"]:
            details["ambiguous_identity"] = id(node) in ambiguous_paths
            if id(node) in ambiguous_paths:
                details["ambiguous_identity_path"] = ambiguous_paths[id(node)]
        events.append({"rule": rule, "path": paths[id(node)] + suffix, "old": old, "new": new, **details})

    # Observe all ignore rules on the same DOM, before any nested matches vanish.
    ignored = {}
    for rule_id, rule in _rule_entries(rules, "ignore_selectors"):
        selector = rule["selector"]
        matches = _select(root, selector)
        observe(rule_id, rule, matches, "parsed", matched_nodes=[
            {"original_path": original_paths[id(node)]} for node in matches])
        for node in matches:
            ignored.setdefault(id(node), (node, []))[1].append({"rule_id": rule_id, "selector": selector})
    protected = {id(root), *collections, *keyed, *identities}
    for node, _ in ignored.values():
        if any(id(n) in protected for n in [node, *node.descendants]):
            raise ContractError(f"{side}: ignore selector removes region, collection, or identity/keyed item")
    # Remove each outermost subtree once, retaining every nested match location.
    for node, matching_rules in ignored.values():
        if any(id(parent) in ignored for parent in node.parents):
            continue
        matched_nodes = [{"original_path": original_paths[id(matched)], "rules": ignored[id(matched)][1]}
                         for matched in [node, *node.descendants] if id(matched) in ignored]
        event("ignore_selectors", node, str(node), None, rules=matching_rules,
              selectors=[entry["selector"] for entry in matching_rules], matched_nodes=matched_nodes)
        node.extract()
    required_attributes("after_ignores")
    required_text_coverage(_required(root, rules, side, "after_ignores", original_paths), "after_ignores")
    # Attribute selection and coverage share the post-ignore, pre-removal DOM.
    targets = {}
    for rule_id, rule in _rule_entries(rules, "volatile_attributes"):
        matches = _select(root, rule["selector"])
        observe(rule_id, rule, matches, "after_ignores",
                attributes_present={attr: sum(attr in node.attrs for node in matches) for attr in rule["attributes"]},
                matched_nodes=[{"original_path": original_paths[id(node)]} for node in matches])
        for node in matches:
            for attr in rule["attributes"]:
                if id(node) in keyed and attr == keyed[id(node)][0]:
                    raise ContractError(f"{side}: cannot normalize item key attribute {attr}")
                if id(node) in identities and attr == identities[id(node)][1]:
                    raise ContractError(f"{side}: cannot normalize sibling identity key attribute {attr}")
                targets.setdefault((id(node), attr), (node, []))[1].append({"rule_id": rule_id, "selector": rule["selector"]})
    for (_, attr), (node, matching_rules) in targets.items():
        event("volatile_attributes", node, node.attrs.pop(attr, None), None, "/@" + _segment(attr),
              rules=matching_rules, selectors=[entry["selector"] for entry in matching_rules],
              original_target_path=original_paths[id(node)])
    required_attributes("after_volatile")
    required_text_coverage(_required(root, rules, side, "after_volatile", original_paths), "after_volatile")

    origins, built_identities = {}, {}
    whitespace_nodes = set()

    def build(node, semantic_path):
        origins[semantic_path] = original_paths[id(node)]
        if id(node) in identities:
            built_identities[semantic_path] = identities[id(node)]
        if not isinstance(node, Tag):
            text = str(node)
            if type(node) is NavigableString and rules["normalize_whitespace"] and not any(p.name in {"pre", "code", "textarea", "script", "style"} for p in node.parents):
                whitespace_nodes.add(id(node))
                normalized = re.sub(r"\s+", " ", text).strip()
                event("normalize_whitespace", node, text, normalized,
                      rules=[{"rule_id": "normalize_whitespace", "selector": None}])
                text = normalized
            if not text and type(node) is NavigableString:
                return None
            return {"kind": "text" if type(node) is NavigableString else type(node).__name__, "text": text}
        result = {"kind": "element", "name": node.name, "attributes": dict(sorted(node.attrs.items()))}
        if id(node) in collections:
            rule_id, rule, items, keys = collections[id(node)]
            event("unordered_collection", node, keys, sorted(keys), key_attribute=rule["key_attribute"],
                  selector=rule["selector"], rules=[{"rule_id": rule_id, "selector": rule["selector"]}])
            result["items"] = {}
            for key, item in sorted(zip(keys, items)):
                part = f"item[{rule['key_attribute']}={json.dumps(key, ensure_ascii=False)}]"
                result["items"][part] = build(item, semantic_path + "/items/" + _segment(part))
            field, children = "separators", [child for child in node.children if id(child) not in keyed]
        else:
            field, children = "children", node.children
        result[field] = []
        for child in children:
            child_path = f"{semantic_path}/{field}/{len(result[field])}"
            built = build(child, child_path)
            if built is not None:
                result[field].append(built)
            else:
                origins.pop(child_path)
        return result

    built = build(root, "/region")
    rows = [coverage[rule_id] for field in ("ignore_selectors", "volatile_attributes", "unordered_collections", "sibling_identity", "required_attributes")
            for rule_id, _ in _rule_entries(rules, field)]
    rows.extend(coverage[f"required_selectors[{index}]"] for index in range(len(rules["required_selectors"])))
    if rules["normalize_whitespace"]:
        whitespace = [e for e in events if e["rule"] == "normalize_whitespace"]
        rows.append({"rule_id": "normalize_whitespace", "text_nodes": len(whitespace),
                     "changed_text_nodes": sum(e["old"] != e["new"] for e in whitespace),
                     # Build visits keyed items in sorted order. The surviving DOM
                     # retains original preorder, including nodes normalized empty.
                     "matched_nodes": [{"original_path": original_paths[id(node)]}
                                       for node in root.descendants if id(node) in whitespace_nodes]})
    return built, events, rows, origins, built_identities, set(ambiguous_paths.values())


def _identity_segment(rule_id, attr, key):
    return f"sibling[{rule_id}:{attr}={json.dumps(key, ensure_ascii=False)}]"


def _diff(old, new, old_origins, new_origins, old_identities, new_identities):
    changes = []

    def source(path, origins):
        if path is None:
            return None
        ancestor = path
        while ancestor not in origins:
            ancestor = ancestor.rsplit("/", 1)[0]
        suffix = path[len(ancestor):]
        if suffix == "/text":
            suffix = ""
        elif suffix.startswith("/attributes/"):
            suffix = "/@" + suffix[len("/attributes/"):]
        return origins[ancestor] + suffix

    def emit(kind, path, old, new, ap, bp, **details):
        changes.append({"kind": kind, "path": path, "old": old, "new": new,
                        "before_path": source(ap, old_origins), "after_path": source(bp, new_origins), **details})

    def sequence(a, b, p, ap, bp, ai, bi):
        # Retain original indices while aligning the unselected residual sequence.
        tokens = lambda seq, indices: [json.dumps(seq[i], sort_keys=True, ensure_ascii=False) for i in indices]
        pairs = []
        for _, i, j, k, l in SequenceMatcher(None, tokens(a, ai), tokens(b, bi), autojunk=False).get_opcodes():
            common = min(j - i, l - k)
            for offset in range(common):
                x, y = ai[i + offset], bi[k + offset]
                pairs.append((x, y))
                # Even equal subtrees may contain ambiguous identities.
                walk(a[x], b[y], f"{p}/{x}", f"{ap}/{x}", f"{bp}/{y}")
            for index in ai[i + common:j]:
                emit("removed", f"{p}/{index}", a[index], None, f"{ap}/{index}", None)
            for index in bi[k + common:l]:
                emit("added", f"{p}/{index}", None, b[index], None, f"{bp}/{index}")
        return pairs

    def siblings(a, b, p, ap, bp):
        groups = [defaultdict(list), defaultdict(list)]
        selected = [set(), set()]
        for side, (seq, prefix, identities) in enumerate(((a, ap, old_identities), (b, bp, new_identities))):
            for index in range(len(seq)):
                identity = identities.get(f"{prefix}/{index}")
                if identity is not None:
                    groups[side][identity].append(index)
                    selected[side].add(index)
        pairs, matched = [], []
        for identity in sorted(groups[0].keys() | groups[1].keys(), key=lambda x: (x[0], x[1], x[2] is not None, x[2] or "")):
            rule_id, attr, key = identity
            ai, bi = (g[identity] for g in groups)
            q = p + "/" + _segment(_identity_segment(*identity))
            details = {"identity": {"rule_id": rule_id, "key_attribute": attr, "key": key}}
            if key is None or len(ai) > 1 or len(bi) > 1:
                candidates = lambda seq, indices, prefix, origins: [
                    {"path": source(f"{prefix}/{i}", origins), "value": seq[i]} for i in indices]
                emit("ambiguous", q, candidates(a, ai, ap, old_origins), candidates(b, bi, bp, new_origins),
                     ap.rsplit("/", 1)[0] if ai else None, bp.rsplit("/", 1)[0] if bi else None,
                     reason="missing_key" if key is None else "duplicate_key", **details)
            elif ai and bi:
                x, y = ai[0], bi[0]
                pairs.append((x, y))
                matched.append((x, y, q, details))
                walk(a[x], b[y], q, f"{ap}/{x}", f"{bp}/{y}")
            elif ai:
                emit("removed", q, a[ai[0]], None, f"{ap}/{ai[0]}", None, **details)
            else:
                emit("added", q, None, b[bi[0]], None, f"{bp}/{bi[0]}", **details)
        pairs.extend(sequence(a, b, p, ap, bp,
                              [i for i in range(len(a)) if i not in selected[0]],
                              [i for i in range(len(b)) if i not in selected[1]]))
        # Compare relative positions among surviving paired siblings, so inserting
        # or removing a peer alone does not produce moves for all later nodes.
        old_order = {pair: rank for rank, pair in enumerate(sorted(pairs))}
        new_order = {pair: rank for rank, pair in enumerate(sorted(pairs, key=lambda pair: pair[1]))}
        for x, y, q, details in matched:
            if old_order[(x, y)] != new_order[(x, y)]:
                emit("moved", q, x, y, f"{ap}/{x}", f"{bp}/{y}", **details)

    def walk(a, b, p, ap, bp):
        if a == b and not (old_identities or new_identities):
            return
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(a.keys() | b.keys()):
                part = "/" + _segment(key)
                q, aq, bq = p + part, ap + part, bp + part
                if key not in a:
                    emit("added", q, None, b[key], None, bq)
                elif key not in b:
                    emit("removed", q, a[key], None, aq, None)
                else:
                    walk(a[key], b[key], q, aq, bq)
        elif isinstance(a, list) and isinstance(b, list):
            if any(f"{ap}/{i}" in old_identities for i in range(len(a))) or any(f"{bp}/{i}" in new_identities for i in range(len(b))):
                siblings(a, b, p, ap, bp)
            else:
                sequence(a, b, p, ap, bp, list(range(len(a))), list(range(len(b))))
        elif a != b:
            emit("changed", p, a, b, ap, bp)

    walk(old, new, "/region", "/region", "/region")
    return changes


def _suppressed(before, after, blocked):
    # A duplicate on either side invalidates pairing on both sides, including a
    # unique counterpart. Keep every observation in the audit, but claim no
    # cross-snapshot suppression beneath that ambiguous identity. Derive blocked
    # paths from the DOM, even when that side has no normalization observations.
    maps = [{(e["rule"], e["path"]): e for e in events
             if not any(e["path"] == prefix or e["path"].startswith(prefix + "/") for prefix in blocked)}
            for events in (before, after)]

    def location(event):
        # Volatile rules observe missing attributes too. Keep that target in the
        # audit, but do not claim an original attribute location that never existed.
        if event is None or (event["rule"] == "volatile_attributes" and event["old"] is None):
            return None
        return event["original_path"]

    result = []
    for key in sorted(maps[0].keys() | maps[1].keys()):
        a, b = (m.get(key) for m in maps)
        # An absent ignored element or attribute has the same normalized value (None).
        if a is None or b is None:
            present = a or b
            if present["rule"] not in {"ignore_selectors", "volatile_attributes"} and not (present["rule"] == "normalize_whitespace" and present["new"] == ""):
                continue
        old_a, old_b = (e["old"] if e else None for e in (a, b))
        absent = "" if key[0] == "normalize_whitespace" else None
        new_a, new_b = (e["new"] if e else absent for e in (a, b))
        if old_a != old_b and new_a == new_b:
            finding = {"rule": key[0], "path": key[1], "old": old_a, "new": old_b, "normalized": new_a,
                       "before_path": location(a), "after_path": location(b),
                       "before_rules": a["rules"] if a else [], "after_rules": b["rules"] if b else []}
            if key[0] == "ignore_selectors":
                # Independent per-side evidence, without pairing nested matches.
                finding.update(before_matched_nodes=a["matched_nodes"] if a else [],
                               after_matched_nodes=b["matched_nodes"] if b else [])
            if key[0] == "volatile_attributes":
                # Retain its observed element without inventing an absent peer.
                finding.update(before_target_path=a["original_target_path"] if a else None,
                               after_target_path=b["original_target_path"] if b else None)
            result.append(finding)
    return result


def compare_html(before: str, after: str, rules: dict) -> dict[str, Any]:
    """Compare Unicode snapshots; invalid input raises ContractError, never unchanged."""
    rules = validate_rules(rules)
    today = _utc_today()
    _check_expiry(rules, today)
    try:
        old, old_events, old_coverage, old_origins, old_identities, old_ambiguous = _prepare(before, rules, "before")
        new, new_events, new_coverage, new_origins, new_identities, new_ambiguous = _prepare(after, rules, "after")
        changes = _diff(old, new, old_origins, new_origins, old_identities, new_identities)
    except (RecursionError, soupsieve.SelectorSyntaxError, NotImplementedError) as exc:
        raise ContractError(f"parse/comparison contract failed: {exc}") from exc
    return {"schema_version": 1, "status": "changed" if changes else "unchanged", "rules": rules,
            "changes": changes, "suppressed": _suppressed(old_events, new_events, old_ambiguous | new_ambiguous),
            "normalizations": {"before": old_events, "after": new_events},
            "rule_coverage": {"evaluated_on": today.isoformat(), "before": old_coverage, "after": new_coverage},
            "scope": {"required_region": rules["required_region"], "outside_region": "not compared", "parser": "BeautifulSoup/html.parser", "comparison": "parsed DOM, not source bytes or browser rendering"}}


def compare_files(before: str | Path, after: str | Path, rules: dict) -> dict:
    result = compare_html(read_utf8(before), read_utf8(after), rules)
    result["inputs"] = {"before": str(before), "after": str(after)}
    return result
