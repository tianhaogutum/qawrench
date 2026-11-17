"""Check one actual response (status, Content-Type, body) against its OpenAPI operation."""
from __future__ import annotations

from collections import namedtuple
import json
import os
import re

from ._js import js_json, js_string, utf8_length, utf8_prefix
from .inputs import parse_url
from .openapi import Router, load_document, pointer_escape

_JSON_MEDIA = re.compile(r"/(?:[^/]+\+)?json$")
_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_QUOTED_VALUE = re.compile(r"[\t\x20-\x7e\x80-\xff]*")
_HTTP_WHITESPACE = " \t\n\r"
MAX_SCHEMA_ISSUES = 20

Media = namedtuple("Media", "type subtype params essence")


class Contract:
    """A loaded OpenAPI document: routing plus cached response-schema validators."""

    def __init__(self, definition: dict, router: Router, registry):
        self.definition = definition
        self.router = router
        self._registry = registry

    def validate(self, pointer: str, body) -> dict:
        return self._registry.validate(pointer, body)


def load_contract(schema_path: str | os.PathLike[str], *, api_root: str = "/") -> Contract:
    """Load, bundle and prepare an OpenAPI 3.0.x/3.1.x document (needs the ``openapi`` extra)."""
    from .schema import SchemaRegistry  # imports jsonschema

    definition, router, prepared, is31 = load_document(schema_path, api_root)
    return Contract(definition, router, SchemaRegistry(prepared, is31))


# ---------------------------------------------------------------------------
# Media types (WHATWG MIME type parsing, as Node's util.MIMEType)

_ENCODING_LABELS = {
    "utf-8": ("unicode-1-1-utf-8", "unicode11utf8", "unicode20utf8", "utf-8", "utf8", "x-unicode20utf8"),
    "windows-1252": ("ansi_x3.4-1968", "ascii", "cp1252", "cp819", "csisolatin1", "ibm819", "iso-8859-1",
                     "iso-ir-100", "iso8859-1", "iso88591", "iso_8859-1", "iso_8859-1:1987", "l1", "latin1",
                     "us-ascii", "windows-1252", "x-cp1252"),
    "utf-16le": ("csunicode", "iso-10646-ucs-2", "ucs-2", "unicode", "unicodefeff", "utf-16", "utf-16le"),
    "utf-16be": ("unicodefffe", "utf-16be"),
    "iso-8859-15": ("csisolatin9", "iso-8859-15", "iso8859-15", "iso885915", "iso_8859-15", "l9"),
}
_ENCODINGS = {label: name for name, labels in _ENCODING_LABELS.items() for label in labels}


def _charset(value: str) -> str:
    """``new TextDecoder(label).encoding`` for common labels; other values compare lowercased."""
    return _ENCODINGS.get(value.strip("\t\n\f\r ").lower(), value.lower())


def _quoted(text: str, position: int) -> tuple[str, int]:
    value = []
    position += 1
    while position < len(text):
        char = text[position]
        position += 1
        if char == "\\":
            if position >= len(text):
                value.append("\\")
                break
            value.append(text[position])
            position += 1
        elif char == '"':
            break
        else:
            value.append(char)
    return "".join(value), position


def parse_mime(value) -> Media | None:
    """Parse a media type; ``None`` when type or subtype is invalid."""
    text = ("undefined" if value is None else js_string(value)).strip(_HTTP_WHITESPACE)
    slash = text.find("/")
    if slash < 0 or not _TOKEN.fullmatch(text[:slash]):
        return None
    kind, rest = text[:slash], text[slash + 1:]
    semicolon = rest.find(";")
    subtype = (rest if semicolon < 0 else rest[:semicolon]).rstrip(_HTTP_WHITESPACE)
    if not _TOKEN.fullmatch(subtype):
        return None
    params: dict[str, str] = {}
    position = len(rest) if semicolon < 0 else semicolon
    while position < len(rest):
        position += 1
        while position < len(rest) and rest[position] in _HTTP_WHITESPACE:
            position += 1
        start = position
        while position < len(rest) and rest[position] not in ";=":
            position += 1
        name = rest[start:position].lower()
        if position < len(rest):
            if rest[position] == ";":
                continue
            position += 1
        if position >= len(rest):
            break
        if rest[position] == '"':
            param_value, position = _quoted(rest, position)
            while position < len(rest) and rest[position] != ";":
                position += 1
        else:
            start = position
            while position < len(rest) and rest[position] != ";":
                position += 1
            param_value = rest[start:position].rstrip(_HTTP_WHITESPACE)
            if not param_value:
                continue
        if name and _TOKEN.fullmatch(name) and _QUOTED_VALUE.fullmatch(param_value) and name not in params:
            params[name] = param_value
    kind, subtype = kind.lower(), subtype.lower()
    return Media(kind, subtype, params, f"{kind}/{subtype}")


def select_media(content: dict, actual) -> dict | None:
    """Pick the response media entry: exact type, then ``type/*``, then ``*/*``; then most parameters."""
    actual_media = parse_mime(actual)
    if actual_media is None or actual_media.type == "*" or "*" in actual_media.subtype:
        return None
    best = None
    for key, media in content.items():
        expected = parse_mime(key)
        if expected is None or (expected.type == "*" and expected.subtype != "*"):
            continue
        if expected.type != "*" and expected.type != actual_media.type:
            continue
        if expected.subtype != "*" and expected.subtype != actual_media.subtype:
            continue
        params = list(expected.params.items())
        matches = True
        for name, value in params:
            actual_value = actual_media.params.get(name)
            if actual_value is None or (_charset(value) != _charset(actual_value) if name == "charset"
                                        else value != actual_value):
                matches = False
                break
        if not matches:
            continue
        specificity = 0 if expected.type == "*" else 1 if expected.subtype == "*" else 2
        if best is None or specificity > best["specificity"] or (
                specificity == best["specificity"] and len(params) > best["parameterCount"]):
            best = {"key": key, "media": media, "essence": actual_media.essence, "specificity": specificity,
                    "parameterCount": len(params)}
        elif specificity == best["specificity"] and len(params) == best["parameterCount"]:
            best["ambiguousKeys"] = [*best.get("ambiguousKeys", [best["key"]]), key]
    return best


# ---------------------------------------------------------------------------
# Response checks


def _reject_constant(name):
    raise ValueError(f"Unexpected token {name} is not valid JSON")


def sample_at(body, pointer: str) -> dict:
    """The JSON value at ``pointer`` as a bounded sample, or ``missing``."""
    value = body
    for raw in pointer.split("/")[1:]:
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and key == "length":
            value = len(value)
        elif isinstance(value, list) and key.isascii() and key.isdigit() and (key == "0" or key[0] != "0") \
                and int(key) < len(value):
            value = value[int(key)]
        else:
            return {"missing": True}
    serialized = js_json(value)
    return {"sample": utf8_prefix(serialized, 256), "sampleTruncated": utf8_length(serialized) > 256}


def check_response(contract: Contract, example: dict, response: dict) -> dict:
    """Validate one actual response; returns the report fields for this case."""
    issues: list[dict] = []
    issue_count = 0
    warnings: list[dict] = []
    pathname = parse_url(example["url"]).path
    operation = contract.router.match_operation(example["method"], pathname)
    if not operation:
        return {"issues": [{"code": "operation-not-found", "path": "",
                            "message": f"No OpenAPI operation for {example['method']} {pathname}"}]}
    label = f"{operation['method'].upper()} {operation['path']}"
    responses = operation.get("responses")
    responses = responses if isinstance(responses, dict) else {}
    status = str(response["status"])
    status_key = next((key for key in (status, f"{status[0]}XX", "default") if key in responses), None)
    if status_key is None:
        return {"operation": label, "issues": [{
            "code": "status", "path": "",
            "message": f"Status {status} is undocumented; expected {', '.join(responses)}"}]}
    expected = responses[status_key] if isinstance(responses[status_key], dict) else {}
    content = expected.get("content")
    content = content if isinstance(content, dict) else {}
    if not content:
        if response["bytes"] > 0 and example["method"] != "HEAD":
            issues.append({"code": "unexpected-body", "path": "",
                           "message": "Response declares no content but handler returned a body"})
        return {"operation": label, "statusKey": status_key, "bodyValidation": "no-content", "issues": issues}
    actual_type = response["headers"].get("content-type")
    selected = select_media(content, actual_type)
    if not selected:
        shown = "(missing)" if actual_type is None else actual_type
        return {"operation": label, "statusKey": status_key, "issues": [{
            "code": "media-type", "path": "",
            "message": f"Content-Type {shown} does not match {', '.join(content)}"}]}
    if selected.get("ambiguousKeys"):
        return {"operation": label, "statusKey": status_key, "issues": [{
            "code": "media-type-ambiguous", "path": "",
            "message": f"Equally specific media entries match this response: {', '.join(selected['ambiguousKeys'])}"}]}
    key = selected["key"]
    if example["method"] == "HEAD":
        return {"operation": label, "statusKey": status_key, "mediaType": key, "bodyValidation": "skipped-head",
                "issues": issues}
    media = selected["media"]
    has_schema = isinstance(media, dict) and "schema" in media
    if _JSON_MEDIA.search(selected["essence"]):
        try:
            body = json.loads(response["body"], parse_constant=_reject_constant)
        except (ValueError, RecursionError) as exc:
            return {"operation": label, "statusKey": status_key, "mediaType": key,
                    "issues": [{"code": "invalid-json", "path": "", "message": str(exc)}]}
    elif selected["essence"].startswith("text/"):
        body = response["body"]
    elif not has_schema:
        return {"operation": label, "statusKey": status_key, "mediaType": key, "bodyValidation": "no-schema",
                "issues": issues}
    else:
        return {"operation": label, "statusKey": status_key, "mediaType": key, "issues": [{
            "code": "unsupported-media", "path": "",
            "message": f"Schema validation for {selected['essence']} is not supported (JSON and text only)"}]}
    if not has_schema:
        return {"operation": label, "statusKey": status_key, "mediaType": key, "bodyValidation": "no-schema",
                "issues": issues}
    try:
        pointer = (f"/paths/{pointer_escape(operation['path'])}/{operation['method']}/responses/"
                   f"{pointer_escape(status_key)}/content/{pointer_escape(key)}/schema")
        validation = contract.validate(pointer, body)
        errors = validation["errors"]
        warnings = validation["warnings"]
        issue_count = len(errors)
        for error in errors[:MAX_SCHEMA_ISSUES]:
            params = error["params"]
            suffix = next((params[name] for name in ("missingProperty", "additionalProperty", "unevaluatedProperty",
                                                     "propertyName") if params.get(name) is not None), None)
            path = error["instancePath"] + ("" if suffix is None else "/" + pointer_escape(js_string(suffix)))
            issues.append({"code": "schema", "path": path, "message": error["message"], "keyword": error["keyword"],
                           "schemaPath": error["schemaPath"], **sample_at(body, path)})
    except ModuleNotFoundError:
        raise
    except Exception as exc:  # A schema that cannot be evaluated is reported, never fatal.
        issues.append({"code": "schema-error", "path": "", "message": str(exc) or type(exc).__name__})
    result = {"operation": label, "statusKey": status_key, "mediaType": key, "bodyValidation": "schema",
              "issueCount": max(issue_count, len(issues)), "issuesTruncated": issue_count > len(issues),
              "issues": issues}
    if warnings:
        result["warnings"] = warnings
    return result
