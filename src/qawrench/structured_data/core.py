"""Extraction, strict input validation, and structural comparison (no network)."""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import extruct
from lxml import html

from .selectors import JSONLD_SELECTOR, source_selection

SYNTAXES = ("json-ld", "opengraph")
STRING_MODES = ("trim", "collapse-whitespace", "casefold")


class ParityError(Exception):
    """An actionable validation/extraction failure with optional source locations."""

    def __init__(self, code: str, message: str, provenance: list[dict] | None = None):
        super().__init__(message)
        self.code = code
        self.provenance = provenance or []

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "provenance": self.provenance}


@dataclass
class Capture:
    summary: dict
    entities: dict[str, dict]
    entity_sources: dict[str, dict]
    graph_metadata: list[dict]
    graph_sources: list[dict]
    opengraph: dict[str, list[str]]
    og_sources: dict[str, list[dict]]


def _escape(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def _token(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _validate_base(base_url: str) -> None:
    try:
        parts = urlsplit(base_url)
        port = parts.port
        if (parts.scheme not in ("http", "https") or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.fragment or "#" in base_url
                or re.search(r"[\s\\\x00-\x1f\x7f]", base_url)
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError("expected an absolute HTTP(S) URL without credentials or fragment")
        if parts.netloc.endswith(":"):
            raise ValueError("empty port")
        host = parts.hostname
        if ":" in host:
            ipaddress.IPv6Address(host)
        else:
            ascii_host = host.encode("idna").decode("ascii").rstrip(".")
            if (len(ascii_host) > 253 or any(
                    not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                    for label in ascii_host.split("."))):
                raise ValueError("invalid DNS hostname")
    except (ValueError, TypeError) as exc:
        raise ParityError("invalid_base_url", f"Invalid --base-url {base_url!r}: {exc}") from exc


def _validate_pointers(paths: tuple[str, ...], label: str = "Set-valued") -> None:
    for path in paths:
        if not isinstance(path, str) or not path.startswith("/") or re.search(r"~(?![01])", path):
            raise ParityError("invalid_config", f"{label} path {path!r} must be an RFC 6901 JSON pointer starting with '/'.")


def _string_policy(normalize: dict[str, str] | None, identity_field: str,
                   set_paths: frozenset[str]) -> dict[str, str]:
    if normalize is None:
        return {}
    if not isinstance(normalize, dict):
        raise ParityError("invalid_config", "normalize must map entity-relative JSON pointers (with optional array '*' tokens) to string modes.")
    _validate_pointers(tuple(normalize), "Normalization")
    for path, mode in normalize.items():
        if mode not in STRING_MODES:
            raise ParityError("invalid_config", f"Unknown normalization mode {mode!r} at {path!r}; choose from {STRING_MODES!r}.")
        if path == "/" + _escape(identity_field):
            raise ParityError("invalid_config", f"Normalization cannot target the pairing identity field at {path!r}.")
        if path in set_paths:
            raise ParityError("invalid_config", f"Pointer {path!r} cannot have both string and set-valued policies.")
    return dict(sorted(normalize.items()))


def _strict_json(text: str, source: dict) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict:
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON property {key!r}")
            # Escaped lone surrogates cannot be emitted as valid UTF-8 reports.
            key.encode("utf-8", errors="strict")
            result[key] = value
        return result

    def constant(value: str) -> Any:
        raise ValueError(f"non-finite JSON number {value}")

    def validate(value: Any) -> None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite JSON number (overflow)")
        if isinstance(value, str):
            value.encode("utf-8", errors="strict")
        elif isinstance(value, dict):
            for item in value.values():
                validate(item)
        elif isinstance(value, list):
            for item in value:
                validate(item)

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
        validate(value)
        return value
    except (ValueError, RecursionError) as exc:
        raise ParityError("invalid_jsonld", f"Invalid strict JSON-LD: {exc}. Check the indicated script; comments/trailing commas are unsupported.", [source]) from exc


def _location(path: Path, script: int, line: int | None, pointer: str = "") -> dict:
    return {"file": str(path), "script_index": script, "line": line, "pointer": pointer}


def _property_shape(value: Any) -> dict:
    """Shallow original extracted shape, before normalization or set comparison."""
    if isinstance(value, list):
        return {"type": "array", "length": len(value)}
    return {"type": {dict: "object", str: "string", int: "number", float: "number",
                     bool: "boolean", type(None): "null"}[type(value)]}


def _literal_types(value: Any) -> list[str]:
    """Coverage only: no context expansion, coercion, or comparison policy."""
    values = [value] if isinstance(value, str) else value
    if (not isinstance(values, list)
            or any(not isinstance(item, str) or not item.strip() for item in values)):
        return []
    return sorted(set(values))


def _extract(path: Path, base_url: str, identity_field: str, syntaxes: tuple[str, ...]) -> Capture:
    try:
        data = path.read_bytes()
    except (OSError, ValueError) as exc:
        raise ParityError("input_io", f"Cannot read {path}: {exc}", [{"file": str(path)}]) from exc
    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise ParityError("input_encoding", f"{path}: invalid UTF-8 at byte {exc.start}; convert the saved capture to UTF-8.", [{"file": str(path), "byte": exc.start}]) from exc
    if "\x00" in text:
        raise ParityError("input_encoding", f"{path}: NUL bytes are unsupported; save HTML as UTF-8.", [{"file": str(path)}])
    try:
        tree = html.fromstring(text, parser=html.HTMLParser(encoding="utf-8", no_network=True))
    except (ValueError, TypeError, html.etree.LxmlError) as exc:
        raise ParityError("invalid_html", f"Cannot parse HTML in {path}: {exc}", [{"file": str(path)}]) from exc

    coverage, expected_og = source_selection(tree, path, syntaxes)
    expected: list[dict] = []
    sources: list[dict] = []
    if "json-ld" in syntaxes:
        # Same selector as extruct 0.18.0; validate before its permissive decoder
        # can accept non-JSON or silently omit falsy/non-object roots.
        for index, script in enumerate(tree.xpath(JSONLD_SELECTOR)):
            source = _location(path, index, script.sourceline)
            value = _strict_json(script.xpath("string()"), source)
            roots = value if isinstance(value, list) else [value]
            for item_index, root in enumerate(roots):
                origin = dict(source, pointer=f"/{item_index}" if isinstance(value, list) else "")
                if not isinstance(root, dict) or not root:
                    raise ParityError("unsupported_jsonld", "Each JSON-LD root/list item must be a nonempty object with an identity or top-level @graph.", [origin])
                expected.append(root)
                sources.append(origin)
    try:
        extracted = extruct.extract(tree, base_url=base_url, syntaxes=list(syntaxes), errors="strict", uniform=False)
    except Exception as exc:
        raise ParityError("extraction_failed", f"extruct failed on {path}: {type(exc).__name__}: {exc}", [{"file": str(path)}]) from exc
    if not isinstance(extracted, dict):
        raise ParityError("extraction_failed", "extruct returned a non-object result.", [{"file": str(path)}])
    for syntax in syntaxes:
        if syntax not in extracted or not isinstance(extracted[syntax], list):
            raise ParityError("extraction_failed", f"extruct returned no valid {syntax} result for {path}.", [{"file": str(path)}])
    roots = extracted.get("json-ld", [])
    if _token(roots) != _token(expected):
        raise ParityError("extraction_failed", f"extruct JSON-LD output did not preserve validated objects in {path}.", sources)

    entities: dict[str, dict] = {}
    entity_sources: dict[str, dict] = {}
    graph_metadata, graph_sources = [], []

    def add_entity(entity: Any, source: dict) -> None:
        if not isinstance(entity, dict):
            raise ParityError("unsupported_jsonld", "Each top-level @graph member must be an object.", [source])
        identity = entity.get(identity_field)
        if not isinstance(identity, str) or not identity.strip():
            raise ParityError("missing_identity", f"Entity requires a nonempty string {identity_field!r}; use --identity-field for a different top-level key.", [source])
        if identity_field == "@id":
            if re.search(r"[\s\\\x00-\x1f\x7f]", identity):
                raise ParityError("invalid_identity", "@id must not contain whitespace, backslashes or control characters.", [source])
            try:
                key = identity if identity.startswith("_:") else urljoin(base_url, identity)
            except ValueError as exc:
                raise ParityError("invalid_identity", f"Cannot resolve @id {identity!r}: {exc}", [source]) from exc
        else:
            key = identity
        if key in entities:
            raise ParityError("duplicate_identity", f"Ambiguous identity {key!r} in {path}; provide unique identities (including after relative @id resolution).", [entity_sources[key], source])
        entities[key] = entity
        entity_sources[key] = source

    for root, source in zip(roots, sources):
        if "@graph" not in root:
            add_entity(root, source)
            continue
        if set(root) - {"@graph", "@context"}:
            raise ParityError("unsupported_jsonld", "Top-level @graph wrappers support only @graph and optional @context; named graphs or additional wrapper properties are unsupported.", [source])
        if not isinstance(root["@graph"], list):
            raise ParityError("unsupported_jsonld", "Top-level @graph must be an array of identified objects.", [source])
        # Retain context even on empty graphs and when members have local contexts.
        graph_metadata.append({k: v for k, v in root.items() if k != "@graph"})
        graph_sources.append(source)
        for index, entity in enumerate(root["@graph"]):
            add_entity(entity, dict(source, pointer=source["pointer"] + f"/@graph/{index}"))

    og: dict[str, list[str]] = {}
    og_sources: dict[str, list[dict]] = {}
    blocks = extracted.get("opengraph", [])
    if "opengraph" in syntaxes and _token(blocks) != _token(expected_og):
        locations = [{"file": str(path), "line": node.sourceline,
                      "property": node.get("property")}
                     for node in tree.xpath('//head/meta[@property and @content]')]
        raise ParityError("extraction_failed", f"extruct OpenGraph output did not preserve selected source metadata in {path}.", locations or [{"file": str(path)}])
    if len(blocks) > 1:
        raise ParityError("unsupported_opengraph", f"Expected at most one OpenGraph head block in {path}.", [{"file": str(path)}])
    for block in blocks:
        if (not isinstance(block, dict) or set(block) != {"namespace", "properties"}
                or not isinstance(block["namespace"], dict) or not isinstance(block["properties"], list)):
            raise ParityError("extraction_failed", "Unexpected extruct OpenGraph shape.", [{"file": str(path)}])
        for pair in block["properties"]:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ParityError("extraction_failed", "Expected OpenGraph property/value pairs.", [{"file": str(path)}])
            prop, value = pair
            if not isinstance(prop, str) or not isinstance(value, str):
                raise ParityError("extraction_failed", "Expected OpenGraph string property/value pairs.", [{"file": str(path)}])
            og.setdefault(prop, []).append(value)
    # Retain namespace definitions as metadata rather than discarding custom prefix changes.
    og_namespaces = blocks[0]["namespace"] if blocks else {}
    for node in tree.xpath("//head/meta[@property and @content]"):
        prop = node.get("property")
        if prop in og:
            og_sources.setdefault(prop, []).append({"file": str(path), "line": node.sourceline, "property": prop})
    if "json-ld" in coverage:
        coverage["json-ld"].update(extracted_roots=len(roots), entities=len(entities),
                                   graph_wrappers=len(graph_metadata), entity_ids=sorted(entities),
                                   entity_types={key: _literal_types(entities[key].get("@type"))
                                                 for key in sorted(entities)},
                                   entity_nonblank_properties={
                                       key: sorted(name for name, value in entities[key].items()
                                                   if isinstance(value, str) and value.strip())
                                       for key in sorted(entities)},
                                   entity_property_shapes={
                                       key: {name: _property_shape(value)
                                             for name, value in sorted(entities[key].items())}
                                       for key in sorted(entities)},
                                   entity_array_nonblank_string_counts={
                                       key: {name: sum(isinstance(item, str) and bool(item.strip()) for item in value)
                                             for name, value in sorted(entities[key].items())
                                             if isinstance(value, list)}
                                       for key in sorted(entities)},
                                   entity_array_nonblank_string_members={
                                       key: {name: sorted({item for item in value
                                                          if isinstance(item, str) and item.strip()})
                                             for name, value in sorted(entities[key].items())
                                             if isinstance(value, list)}
                                       for key in sorted(entities)},
                                   entity_array_distinct_nonblank_string_counts={
                                       key: {name: len({item for item in value
                                                       if isinstance(item, str) and item.strip()})
                                             for name, value in sorted(entities[key].items())
                                             if isinstance(value, list)}
                                       for key in sorted(entities)})
    if "opengraph" in coverage:
        coverage["opengraph"].update(extracted_values=sum(map(len, og.values())), blocks=len(blocks),
                                     property_counts={prop: len(values) for prop, values in sorted(og.items())},
                                     nonblank_property_counts={prop: sum(bool(value.strip()) for value in values)
                                                              for prop, values in sorted(og.items())})
    summary = {"file": str(path), "sha256": hashlib.sha256(data).hexdigest(), "coverage": coverage,
               "jsonld_entities": len(entities), "graph_wrappers": len(graph_metadata),
               "opengraph_values": sum(map(len, og.values())), "opengraph_namespaces": og_namespaces}
    return Capture(summary, entities, entity_sources, graph_metadata, graph_sources, og, og_sources)


def _normalization_targets(entity: dict, policy: dict[str, str],
                           set_paths: frozenset[str]) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Resolve policies against original positions, retaining selector ownership.

    Whole '*' tokens expand arrays, but retain RFC 6901 literal-key meaning on
    objects. Missing keys/indices contribute no matches. No mode takes priority
    over another, including identical modes at overlapping selectors.
    """
    owners: dict[str, str] = {}
    concrete: dict[str, str] = {}
    matches: dict[str, list[str]] = {selector: [] for selector in policy}
    for selector, mode in policy.items():
        tokens = [part.replace("~1", "/").replace("~0", "~") for part in selector[1:].split("/")]

        def fail(path: str, reason: str) -> None:
            raise ParityError("invalid_config", f"Normalization pointer {selector!r} at {path!r}: {reason}.",
                              [{"entity_pointer": path, "normalization_pointer": selector}])

        def visit(value: Any, index: int, path: str, expanded: bool = False) -> None:
            if index == len(tokens):
                if path in owners:
                    fail(path, f"overlap with normalization pointer {owners[path]!r}; choose one policy per value")
                if path in set_paths:
                    fail(path, "cannot have both string and set-valued policies")
                if value is not None and not isinstance(value, str):
                    fail(path, "targets a non-string, non-null value")
                owners[path] = selector
                concrete[path] = mode
                matches[selector].append(path)
                return
            token = tokens[index]
            if token == "*" and isinstance(value, list):
                for item_index, item in enumerate(value):
                    visit(item, index + 1, path + f"/{item_index}", True)
            elif token == "*" and not (isinstance(value, dict) and token in value):
                fail(path, "'*' requires an array or an object with a literal '*' key")
            elif isinstance(value, dict):
                if token in value:
                    visit(value[token], index + 1, path + "/" + _escape(token), expanded)
            elif isinstance(value, list):
                # RFC 6901 indices are canonical decimal tokens, never '-'.
                if re.fullmatch(r"0|[1-9][0-9]*", token):
                    # A long out-of-range token must not hit Python's int limit.
                    if len(token) <= len(str(len(value))) and int(token) < len(value):
                        visit(value[int(token)], index + 1, path + "/" + token, expanded)
            elif expanded:
                fail(path, "cannot traverse a scalar inside an expanded array member")

        visit(entity, 0, "")
    return concrete, matches


def _normalize(value: Any, path: str, set_paths: frozenset[str],
               string_policy: dict[str, str], matched: set[str]) -> Any:
    if path in string_policy:
        matched.add(path)
        # Null remains a distinct value. Absence is never filled in by a policy.
        if value is None:
            return None
        if not isinstance(value, str):
            raise ParityError("invalid_config", f"Normalization pointer {path!r} targets a non-string, non-null value.")
        mode = string_policy[path]
        if mode == "trim":
            return value.strip()
        if mode == "collapse-whitespace":
            return " ".join(value.split())
        return value.casefold()
    if path in set_paths:
        if path.endswith("/@list"):
            raise ParityError("invalid_config", f"Set-valued pointer {path!r} targets an ordered JSON-LD @list; order and duplicates must be preserved.")
        if not isinstance(value, list):
            raise ParityError("invalid_config", f"Set-valued pointer {path!r} targets a non-array value.")
        matched.add(path)
    if isinstance(value, dict):
        return {k: _normalize(v, path + "/" + _escape(k), set_paths, string_policy, matched)
                for k, v in sorted(value.items())}
    if isinstance(value, list):
        items = [_normalize(v, path + f"/{i}", set_paths, string_policy, matched) for i, v in enumerate(value)]
        if path in set_paths:
            unique = {_token(v): v for v in items}
            return [unique[k] for k in sorted(unique)]
        return items
    return value


def _diff(old: Any, new: Any, path: str = "") -> list[dict]:
    if _token(old) == _token(new):
        return []
    changes = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(old.keys() | new.keys()):
            child = path + "/" + _escape(key)
            if key not in new:
                changes.append({"kind": "removed", "path": child, "old": old[key]})
            elif key not in old:
                changes.append({"kind": "added", "path": child, "new": new[key]})
            else:
                changes.extend(_diff(old[key], new[key], child))
    elif isinstance(old, list) and isinstance(new, list):
        for index in range(max(len(old), len(new))):
            child = path + f"/{index}"
            if index >= len(new):
                changes.append({"kind": "removed", "path": child, "old": old[index]})
            elif index >= len(old):
                changes.append({"kind": "added", "path": child, "new": new[index]})
            else:
                changes.extend(_diff(old[index], new[index], child))
    else:
        changes.append({"kind": "changed", "path": path, "old": old, "new": new})
    return changes


def compare_files(old_file: str | Path, new_file: str | Path, *, base_url: str,
                  identity_field: str = "@id", syntaxes: tuple[str, ...] = SYNTAXES,
                  set_valued: tuple[str, ...] = (), normalize: dict[str, str] | None = None,
                  baseline: str | Path | None = None) -> dict:
    """Return a JSON-serializable report; raise ParityError on invalid captures/config.

    @id resolution pairs entities only. Entity-relative normalize pointers with
    optional array '*' tokens opt strings into trim, collapse-whitespace, or
    casefold. Null/absence stay distinct. Set-valued pointers cannot reorder
    explicit @list arrays.
    A baseline accepts exact saved deltas only after hash/policy/coverage checks.
    """
    try:
        report = _compare_files(old_file, new_file, base_url=base_url, identity_field=identity_field,
                                syntaxes=syntaxes, set_valued=set_valued, normalize=normalize)
        if baseline is not None:
            from .baseline import apply_baseline
            return apply_baseline(report, Path(baseline))
        return report
    except RecursionError as exc:
        raise ParityError("input_too_deep", "Capture nesting exceeds the supported Python recursion depth; simplify the JSON-LD.") from exc


def _compare_files(old_file: str | Path, new_file: str | Path, *, base_url: str,
                   identity_field: str, syntaxes: tuple[str, ...], set_valued: tuple[str, ...],
                   normalize: dict[str, str] | None) -> dict:
    _validate_base(base_url)
    if not syntaxes or len(set(syntaxes)) != len(syntaxes) or any(s not in SYNTAXES for s in syntaxes):
        raise ParityError("unsupported_syntax", "Only unique selections from 'json-ld' and 'opengraph' are supported; RDFa is unsupported.")
    if not isinstance(identity_field, str) or not identity_field.strip():
        raise ParityError("invalid_config", "--identity-field must be a nonempty top-level field name.")
    _validate_pointers(set_valued)
    set_paths = frozenset(set_valued)
    string_policy = _string_policy(normalize, identity_field, set_paths)
    old = _extract(Path(old_file), base_url, identity_field, syntaxes)
    new = _extract(Path(new_file), base_url, identity_field, syntaxes)
    matched: set[str] = set()
    normalization_matches = {selector: {"old": [], "new": []} for selector in string_policy}
    changes = []
    for identity in sorted(old.entities.keys() | new.entities.keys()):
        def entity_value(capture: Capture, side: str) -> Any:
            if identity not in capture.entities:
                return None
            try:
                concrete, matches = _normalization_targets(capture.entities[identity], string_policy, set_paths)
                for selector, paths in matches.items():
                    if paths:
                        matched.add(selector)
                    normalization_matches[selector][side].extend(
                        {"entity": identity, "path": path} for path in paths)
                return _normalize(capture.entities[identity], "", set_paths, concrete, matched)
            except ParityError as exc:
                locations = [dict(capture.entity_sources[identity], **location) for location in exc.provenance]
                raise ParityError(exc.code, str(exc), locations or [capture.entity_sources[identity]]) from exc

        left, right = entity_value(old, "old"), entity_value(new, "new")
        if identity not in new.entities:
            deltas = [{"kind": "removed_entity", "path": "", "old": left}]
        elif identity not in old.entities:
            deltas = [{"kind": "added_entity", "path": "", "new": right}]
        else:
            deltas = _diff(left, right)
        for delta in deltas:
            changes.append({"syntax": "json-ld", "scope": "entity", "entity": identity, **delta,
                            "old_provenance": old.entity_sources.get(identity), "new_provenance": new.entity_sources.get(identity)})
    unused = set_paths - matched
    if unused:
        raise ParityError("invalid_config", f"Set-valued pointers matched no JSON-LD arrays in either capture: {sorted(unused)!r}.")
    unused = string_policy.keys() - matched
    if unused:
        raise ParityError("invalid_config", f"Normalization pointers matched no JSON-LD values in either capture: {sorted(unused)!r}.")
    for delta in _diff(old.graph_metadata, new.graph_metadata):
        changes.append({"syntax": "json-ld", "scope": "graph_metadata", "entity": None, **delta,
                        "old_provenance": old.graph_sources, "new_provenance": new.graph_sources})
    for delta in _diff(old.opengraph, new.opengraph):
        changes.append({"syntax": "opengraph", "scope": "properties", "entity": None, **delta,
                        "old_provenance": old.og_sources, "new_provenance": new.og_sources})
    for delta in _diff(old.summary["opengraph_namespaces"], new.summary["opengraph_namespaces"]):
        changes.append({"syntax": "opengraph", "scope": "namespaces", "entity": None, **delta})
    return {"schema_version": 1, "tool": "structured-data-parity", "extractor": "extruct==0.18.0",
            "status": "different" if changes else "equal", "base_url": base_url,
            "policy": {"syntaxes": list(syntaxes), "identity_field": identity_field,
                       "relative_id": "resolve-for-identity-only", "set_valued": sorted(set_paths),
                       "normalize": string_policy},
            "captures": {"old": copy.deepcopy(old.summary), "new": copy.deepcopy(new.summary)},
            "normalization_matches": normalization_matches,
            "change_count": len(changes), "changes": changes, "errors": []}
