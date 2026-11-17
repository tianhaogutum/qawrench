"""Response body validation with ``jsonschema``, reported in Ajv's error shape.

OpenAPI 3.0 schemas (converted to draft-04) use a draft-04 validator whose
``$ref`` siblings still apply and whose ``integer`` accepts ``1.0``, matching
Ajv 8 with ``ajv-draft-04``; OpenAPI 3.1 schemas use the 2020-12 validator.
Formats follow ``ajv-formats`` (full mode) plus the OpenAPI ``int32``, ``int64``,
``float``, ``double``, ``byte``, ``binary`` and ``password`` registrations the
original made. Unknown formats are accepted and reported as warnings, as Ajv
does with ``strict: false``. Messages use Ajv's English wording so text and JSON
reports keep their original form.
"""
from __future__ import annotations

from operator import methodcaller
import re
from urllib.parse import quote, unquote

import jsonschema
from jsonschema import Draft4Validator, Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
import referencing
import referencing.jsonschema

from ._js import js_number
from .errors import CheckError
from .openapi import pointer_escape, pointer_get, visit_schema

DOCUMENT_URI = "https://qawrench.invalid/msw-contract/document.json"

try:  # Used only to split one combined unevaluatedProperties error into Ajv's per-property errors.
    from jsonschema._utils import find_evaluated_property_keys_by_schema as _evaluated_keys
except ImportError:  # pragma: no cover - depends on the installed jsonschema
    _evaluated_keys = None


# ---------------------------------------------------------------------------
# Formats (ajv-formats "full" definitions, re-expressed for Python's re module)

_DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_DAYS = (0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
_TIME = re.compile(r"([0-9]{2}):([0-9]{2}):([0-9]{2}(?:\.[0-9]+)?)(z|([+-])([0-9]{2})(?::?([0-9]{2}))?)?",
                   re.IGNORECASE)
_DATE_TIME_SEPARATOR = re.compile(r"t|\s", re.IGNORECASE)
_IPV4_PART = r"(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])"
_URI_IPV6 = (r"(?:(?:(?:(?:[0-9a-f]{1,4}:){6}|::(?:[0-9a-f]{1,4}:){5}|(?:[0-9a-f]{1,4})?::(?:[0-9a-f]{1,4}:){4}"
             r"|(?:(?:[0-9a-f]{1,4}:){0,1}[0-9a-f]{1,4})?::(?:[0-9a-f]{1,4}:){3}"
             r"|(?:(?:[0-9a-f]{1,4}:){0,2}[0-9a-f]{1,4})?::(?:[0-9a-f]{1,4}:){2}"
             r"|(?:(?:[0-9a-f]{1,4}:){0,3}[0-9a-f]{1,4})?::[0-9a-f]{1,4}:"
             r"|(?:(?:[0-9a-f]{1,4}:){0,4}[0-9a-f]{1,4})?::)"
             r"(?:[0-9a-f]{1,4}:[0-9a-f]{1,4}|(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}"
             r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?))"
             r"|(?:(?:[0-9a-f]{1,4}:){0,5}[0-9a-f]{1,4})?::[0-9a-f]{1,4}"
             r"|(?:(?:[0-9a-f]{1,4}:){0,6}[0-9a-f]{1,4})?::)"
             r"|[Vv][0-9a-f]+\.[a-z0-9\-._~!$&'()*+,;=:]+)")
_URI_HOST = (r"(?:\[" + _URI_IPV6 + r"\]|(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}"
             r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)|(?:[a-z0-9\-._~!$&'{q}()*+,;=]|%[0-9a-f]{{2}})*)")


def _uri_pattern(scheme: str, quote_char: str) -> re.Pattern:
    q = quote_char
    host = _URI_HOST.replace("{q}", q).replace("{{2}}", "{2}")
    pchar = rf"(?:[a-z0-9\-._~!$&'{q}()*+,;=:@]|%[0-9a-f]{{2}})"
    return re.compile(
        scheme
        + rf"(?:\/?\/(?:(?:[a-z0-9\-._~!$&'()*+,;=:]|%[0-9a-f]{{2}})*@)?{host}(?::[0-9]*)?(?:\/{pchar}*)*"
        + rf"|\/(?:{pchar}+(?:\/{pchar}*)*)?|{pchar}+(?:\/{pchar}*)*)"
        + rf"(?:\?(?:[a-z0-9\-._~!$&'{q}()*+,;=:@/?]|%[0-9a-f]{{2}})*)?"
        + rf"(?:#(?:[a-z0-9\-._~!$&'{q}()*+,;=:@/?]|%[0-9a-f]{{2}})*)?",
        re.IGNORECASE)


_URI = _uri_pattern(r"(?:[a-z][a-z0-9+\-.]*:)", "")
_URI_REFERENCE = _uri_pattern(r"(?:[a-z][a-z0-9+\-.]*:)?", '"')
_NOT_URI_FRAGMENT = re.compile(r"/|:")
_STRING_FORMATS = {
    "duration": re.compile(r"P(?!$)((\d+Y)?(\d+M)?(\d+D)?(T(?=\d)(\d+H)?(\d+M)?(\d+S)?)?|(\d+W)?)", re.ASCII),
    "uri-reference": _URI_REFERENCE,
    "uri-template": re.compile(
        r"(?:(?:[^\x00-\x20\"'<>%\\^`{|}]|%[0-9a-f]{2})|\{[+#./;?&=,!@|]?(?:[a-z0-9_]|%[0-9a-f]{2})+"
        r"(?::[1-9][0-9]{0,3}|\*)?(?:,(?:[a-z0-9_]|%[0-9a-f]{2})+(?::[1-9][0-9]{0,3}|\*)?)*\})*",
        re.IGNORECASE),
    "url": re.compile(
        r"(?:https?|ftp):\/\/(?:\S+(?::\S*)?@)?(?:(?!(?:10|127)(?:\.[0-9]{1,3}){3})"
        r"(?!(?:169\.254|192\.168)(?:\.[0-9]{1,3}){2})(?!172\.(?:1[6-9]|2[0-9]|3[0-1])(?:\.[0-9]{1,3}){2})"
        r"(?:[1-9][0-9]?|1[0-9][0-9]|2[01][0-9]|22[0-3])(?:\.(?:1?[0-9]{1,2}|2[0-4][0-9]|25[0-5])){2}"
        r"(?:\.(?:[1-9][0-9]?|1[0-9][0-9]|2[0-4][0-9]|25[0-4]))"
        r"|(?:(?:[a-z0-9\u00a1-\uffff]+-)*[a-z0-9\u00a1-\uffff]+)(?:\.(?:[a-z0-9\u00a1-\uffff]+-)*"
        r"[a-z0-9\u00a1-\uffff]+)*(?:\.(?:[a-z\u00a1-\uffff]{2,})))(?::[0-9]{2,5})?(?:\/[^\s]*)?",
        re.IGNORECASE),
    "email": re.compile(
        r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*@"
        r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", re.IGNORECASE),
    "hostname": re.compile(
        r"(?=.{1,253}\.?$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[-0-9a-z]{0,61}[0-9a-z])?)*\.?",
        re.IGNORECASE),
    "ipv4": re.compile(rf"(?:{_IPV4_PART}\.){{3}}{_IPV4_PART}"),
    "uuid": re.compile(r"(?:urn:uuid:)?[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", re.IGNORECASE),
    "json-pointer": re.compile(r"(?:\/(?:[^~/]|~0|~1)*)*"),
    "json-pointer-uri-fragment": re.compile(r"#(?:\/(?:[a-z0-9_\-.!$&'()*+,;:=@]|%[0-9a-f]{2}|~0|~1)*)*",
                                            re.IGNORECASE),
    "relative-json-pointer": re.compile(r"(?:0|[1-9][0-9]*)(?:#|(?:\/(?:[^~/]|~0|~1)*)*)"),
    # OpenAPI base64, as registered by the original tool.
    "byte": re.compile(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?"),
}
_IPV6_GROUP = r"[0-9a-f]{1,4}"
_IPV4_DOTTED = rf"{_IPV4_PART}(?:\.{_IPV4_PART}){{3}}"
_STRING_FORMATS["ipv6"] = re.compile(
    rf"((({_IPV6_GROUP}:){{7}}({_IPV6_GROUP}|:))|(({_IPV6_GROUP}:){{6}}(:{_IPV6_GROUP}|{_IPV4_DOTTED}|:))"
    rf"|(({_IPV6_GROUP}:){{5}}(((:{_IPV6_GROUP}){{1,2}})|:{_IPV4_DOTTED}|:))"
    rf"|(({_IPV6_GROUP}:){{4}}(((:{_IPV6_GROUP}){{1,3}})|((:{_IPV6_GROUP})?:{_IPV4_DOTTED})|:))"
    rf"|(({_IPV6_GROUP}:){{3}}(((:{_IPV6_GROUP}){{1,4}})|((:{_IPV6_GROUP}){{0,2}}:{_IPV4_DOTTED})|:))"
    rf"|(({_IPV6_GROUP}:){{2}}(((:{_IPV6_GROUP}){{1,5}})|((:{_IPV6_GROUP}){{0,3}}:{_IPV4_DOTTED})|:))"
    rf"|(({_IPV6_GROUP}:){{1}}(((:{_IPV6_GROUP}){{1,6}})|((:{_IPV6_GROUP}){{0,4}}:{_IPV4_DOTTED})|:))"
    rf"|(:(((:{_IPV6_GROUP}){{1,7}})|((:{_IPV6_GROUP}){{0,5}}:{_IPV4_DOTTED})|:)))",
    re.IGNORECASE)
KNOWN_FORMATS = frozenset({
    "date", "time", "date-time", "iso-time", "iso-date-time", "duration", "uri", "uri-reference",
    "uri-template", "url", "email", "hostname", "ipv4", "ipv6", "regex", "uuid", "json-pointer",
    "json-pointer-uri-fragment", "relative-json-pointer", "byte", "int32", "int64", "float", "double",
    "password", "binary",
})


def _is_date(text: str) -> bool:
    match = _DATE.fullmatch(text)
    if not match:
        return False
    year, month, day = (int(part) for part in match.groups())
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    return 1 <= month <= 12 and 1 <= day <= (29 if month == 2 and leap else _DAYS[month])


def _is_time(text: str, strict_zone: bool) -> bool:
    match = _TIME.fullmatch(text)
    if not match:
        return False
    hour, minute, second = int(match[1]), int(match[2]), float(match[3])
    zone = match[4]
    sign = -1 if match[5] == "-" else 1
    zone_hour, zone_minute = int(match[6] or 0), int(match[7] or 0)
    if zone_hour > 23 or zone_minute > 59 or (strict_zone and not zone):
        return False
    if hour <= 23 and minute <= 59 and second < 60:
        return True
    utc_minute = minute - zone_minute * sign
    utc_hour = hour - zone_hour * sign - (1 if utc_minute < 0 else 0)
    return utc_hour in (23, -1) and utc_minute in (59, -1) and second < 61  # leap second


def _is_date_time(text: str, strict_zone: bool) -> bool:
    parts = _DATE_TIME_SEPARATOR.split(text)
    return len(parts) == 2 and _is_date(parts[0]) and _is_time(parts[1], strict_zone)


def _is_regex(text: str) -> bool:
    if re.search(r"[^\\]\\Z", text):
        return False
    try:
        re.compile(text)
    except (re.error, OverflowError, RecursionError):
        return False
    return True


def _number(instance) -> bool:
    return isinstance(instance, (int, float)) and not isinstance(instance, bool)


def _integral(instance) -> bool:
    return (isinstance(instance, int) and not isinstance(instance, bool)) or \
        (isinstance(instance, float) and instance.is_integer())


def _format_checker() -> FormatChecker:
    checker = FormatChecker(formats=())

    def string_format(name, predicate):
        checker.checks(name)(lambda instance: not isinstance(instance, str) or predicate(instance))

    for name, pattern in _STRING_FORMATS.items():
        string_format(name, lambda text, pattern=pattern: pattern.fullmatch(text) is not None)
    string_format("date", _is_date)
    string_format("time", lambda text: _is_time(text, True))
    string_format("date-time", lambda text: _is_date_time(text, True))
    string_format("iso-time", lambda text: _is_time(text, False))
    string_format("iso-date-time", lambda text: _is_date_time(text, False))
    string_format("uri", lambda text: bool(_NOT_URI_FRAGMENT.search(text)) and _URI.fullmatch(text) is not None)
    string_format("regex", _is_regex)
    for name in ("password", "binary"):
        checker.checks(name)(lambda instance: True)
    checker.checks("int32")(lambda value: not _number(value) or (
        _integral(value) and -2147483648 <= value <= 2147483647))
    checker.checks("int64")(lambda value: not _number(value) or (
        _integral(value) and -(2 ** 63) <= value < 2 ** 63))
    for name in ("float", "double"):
        checker.checks(name)(lambda value: True)
    return checker


FORMAT_CHECKER = _format_checker()


# ---------------------------------------------------------------------------
# Keywords whose errors carry Ajv params (missingProperty, additionalProperty, ...)


def _ajv_error(message: str, **params) -> ValidationError:
    error = ValidationError(message)
    error.ajv_message = message
    error.ajv_params = params
    return error


def _required(validator, required, instance, schema):
    if not validator.is_type(instance, "object"):
        return
    for name in required:
        if name not in instance:
            yield _ajv_error(f"must have required property '{name}'", missingProperty=name)


def _additional_properties(validator, additional, instance, schema):
    if not validator.is_type(instance, "object"):
        return
    properties = schema.get("properties") or {}
    patterns = schema.get("patternProperties") or {}
    extras = [name for name in instance
              if name not in properties and not any(re.search(pattern, name) for pattern in patterns)]
    if validator.is_type(additional, "object"):
        for name in extras:
            yield from validator.descend(instance[name], additional, path=name)
    elif additional is False:
        for name in extras:
            yield _ajv_error("must NOT have additional properties", additionalProperty=name)


_ORIGINAL_UNEVALUATED = Draft202012Validator.VALIDATORS["unevaluatedProperties"]


def _unevaluated_properties(validator, unevaluated, instance, schema):
    errors = list(_ORIGINAL_UNEVALUATED(validator, unevaluated, instance, schema))
    if unevaluated is not False or not errors or _evaluated_keys is None:
        yield from errors
        return
    evaluated = _evaluated_keys(validator, instance, schema)
    for name in instance:
        if name not in evaluated:
            yield _ajv_error("must NOT have unevaluated properties", unevaluatedProperty=name)


def _property_names(validator, names_schema, instance, schema):
    if not validator.is_type(instance, "object"):
        return
    for name in instance:
        if next(iter(validator.descend(instance=name, schema=names_schema)), None) is not None:
            yield _ajv_error("property name must be valid", propertyName=name)


def _integer(checker, instance) -> bool:
    return _integral(instance)


_DRAFT4_ID_OF = getattr(Draft4Validator, "ID_OF", None) or referencing.jsonschema.DRAFT4.id_of
_Draft4 = jsonschema.validators.create(
    meta_schema=Draft4Validator.META_SCHEMA,
    validators={**Draft4Validator.VALIDATORS, "required": _required,
                "additionalProperties": _additional_properties},
    type_checker=Draft4Validator.TYPE_CHECKER.redefine("integer", _integer),
    id_of=_DRAFT4_ID_OF,
    # Ajv 8 applies keywords next to "$ref" in every draft; jsonschema's draft-04 ignores them.
    applicable_validators=methodcaller("items"),
)
_Draft2020 = jsonschema.validators.extend(Draft202012Validator, validators={
    "required": _required, "additionalProperties": _additional_properties,
    "unevaluatedProperties": _unevaluated_properties, "propertyNames": _property_names,
})


# ---------------------------------------------------------------------------
# Ajv-style messages

_LIMIT_MESSAGES = {
    "minLength": "must NOT have fewer than {} characters", "maxLength": "must NOT have more than {} characters",
    "minItems": "must NOT have fewer than {} items", "maxItems": "must NOT have more than {} items",
    "minProperties": "must NOT have fewer than {} properties",
    "maxProperties": "must NOT have more than {} properties",
}
_FIXED_MESSAGES = {
    "enum": "must be equal to one of the allowed values", "const": "must be equal to constant",
    "anyOf": "must match a schema in anyOf", "oneOf": "must match exactly one schema in oneOf",
    "not": "must NOT be valid",
}


def _canonical(value):
    if isinstance(value, dict):
        return ("o", tuple(sorted((key, _canonical(item)) for key, item in value.items())))
    if isinstance(value, list):
        return ("a", tuple(_canonical(item) for item in value))
    if isinstance(value, bool) or value is None:
        return ("p", value)
    if isinstance(value, float) and value.is_integer():
        return ("n", int(value))
    return ("n" if isinstance(value, (int, float)) else "s", value)


def _duplicate_message(items) -> str:
    seen: dict = {}
    for index in range(len(items) - 1, -1, -1):
        key = _canonical(items[index])
        if key in seen:
            return f"must NOT have duplicate items (items ## {seen[key]} and {index} are identical)"
        seen[key] = index
    return "must NOT have duplicate items"


def _message(error: ValidationError, draft4: bool) -> str:
    custom = getattr(error, "ajv_message", None)
    if custom:
        return custom
    keyword, value = error.validator, error.validator_value
    schema = error.schema if isinstance(error.schema, dict) else {}
    if keyword is None:
        return "boolean schema is false"
    if keyword == "type":
        return "must be " + (",".join(map(str, value)) if isinstance(value, list) else str(value))
    if keyword in ("minimum", "maximum"):
        exclusive = draft4 and schema.get("exclusiveMinimum" if keyword == "minimum" else "exclusiveMaximum") is True
        operator = (">" if exclusive else ">=") if keyword == "minimum" else ("<" if exclusive else "<=")
        return f"must be {operator} {js_number(value)}"
    if keyword in ("exclusiveMinimum", "exclusiveMaximum"):
        return f"must be {'>' if keyword == 'exclusiveMinimum' else '<'} {js_number(value)}"
    if keyword in _LIMIT_MESSAGES:
        return _LIMIT_MESSAGES[keyword].format(js_number(value))
    if keyword in _FIXED_MESSAGES:
        return _FIXED_MESSAGES[keyword]
    if keyword == "pattern":
        return f'must match pattern "{value}"'
    if keyword == "format":
        return f'must match format "{value}"'
    if keyword == "multipleOf":
        return f"must be multiple of {js_number(value)}"
    if keyword == "uniqueItems" and isinstance(error.instance, list):
        return _duplicate_message(error.instance)
    if keyword == "items":
        prefix = schema.get("prefixItems")
        return f"must NOT have more than {len(prefix) if isinstance(prefix, list) else 0} items"
    if keyword == "additionalItems":
        items = schema.get("items")
        return f"must NOT have more than {len(items) if isinstance(items, list) else 0} items"
    if keyword == "contains":
        return f"must contain at least {js_number(schema.get('minContains', 1))} valid item(s)"
    return error.message


def _flatten(errors):
    # Ajv with allErrors keeps the failing branches' errors ahead of the anyOf/oneOf error.
    for error in errors:
        if error.validator in ("anyOf", "oneOf") and error.context:
            yield from _flatten(error.context)
        yield error


def _pointer(parts) -> str:
    return "".join("/" + pointer_escape(str(part)) for part in parts)


class SchemaRegistry:
    """One registry for the whole prepared document; validators are cached per pointer."""

    def __init__(self, document: dict, is31: bool):
        specification = referencing.jsonschema.DRAFT202012 if is31 else referencing.jsonschema.DRAFT4
        self.document = document
        self.draft4 = not is31
        self.validator_class = _Draft2020 if is31 else _Draft4
        self.registry = referencing.Registry().with_resource(DOCUMENT_URI, specification.create_resource(document))
        self.cache: dict[str, tuple] = {}

    def _compile(self, pointer: str):
        try:
            target = pointer_get(self.document, pointer)
        except LookupError:
            raise CheckError(f"No response schema at {pointer}") from None
        fragment = "/".join(quote(segment, safe="") for segment in pointer.split("/"))
        validator = self.validator_class({"$ref": f"{DOCUMENT_URI}#{fragment}"}, registry=self.registry,
                                         format_checker=FORMAT_CHECKER)
        messages = dict.fromkeys(_unknown_formats(target, self.document))
        warnings = [{"code": "schema-warning", "message": message} for message in messages]
        return validator, warnings

    def validate(self, pointer: str, body) -> dict:
        entry = self.cache.get(pointer)
        if entry is None:
            entry = self.cache[pointer] = self._compile(pointer)
        validator, warnings = entry
        errors = []
        for error in _flatten(validator.iter_errors(body)):
            errors.append({
                "instancePath": _pointer(error.absolute_path),
                "keyword": "false schema" if error.validator is None else error.validator,
                "message": _message(error, self.draft4),
                "schemaPath": "#" + _pointer(error.absolute_schema_path),
                "params": getattr(error, "ajv_params", None) or {},
            })
        return {"errors": errors, "warnings": warnings}


def _unknown_formats(schema, document):
    """Ajv's ``unknown format "x" ignored in schema at path "#..."`` warnings for reachable schemas."""
    seen_refs: set[str] = set()
    found: list[str] = []

    def scan(root, base: str):
        pending: list[str] = []

        def collect(node):
            fmt = node.get("format")
            if isinstance(fmt, str) and fmt not in KNOWN_FORMATS:
                found.append(f'unknown format "{fmt}" ignored in schema at path "{base}"')
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/"):
                pending.append(ref)

        visit_schema(root, collect)
        for ref in pending:
            if ref in seen_refs:
                continue
            seen_refs.add(ref)
            try:
                target = pointer_get(document, unquote(ref[1:]))
            except LookupError:
                continue
            scan(target, ref)

    scan(schema, "#")
    return found
