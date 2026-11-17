"""OpenAPI 3.0/3.1 document preparation.

The original tool composed several npm libraries for this stage. This module
reimplements the parts of their behaviour that the checks rely on:

* local multi-file bundling with a bounded, local-only resolver and literal
  example data (Redocly ``bundle`` with ``doNotResolveExamples``);
* dereferencing of OpenAPI objects while schema references stay intact
  (Swagger Parser ``dereference`` with ``circular: "ignore"`` and an excluded
  path matcher);
* OpenAPI 3.0 schema conversion to JSON Schema draft-04
  (``@openapi-contrib/openapi-schema-to-json-schema`` 4.0.0, ``removeWriteOnly``);
* operation routing (``openapi-backend``'s ``OpenAPIRouter``, apiRoot stripping,
  exact paths before templates, longest literal template first);
* writeOnly lookups through ``#/`` references (``jsonpointer``) and the 2020-12
  aware schema walk (``json-schema-traverse`` with extended keyword tables).
"""
from __future__ import annotations

import copy
import os
import re
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from ._js import js_json
from .errors import CheckError
from .inputs import MAX_INPUT_BYTES, read_data

METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
OAS_DIALECTS = ("https://json-schema.org/draft/2020-12/schema", "https://spec.openapis.org/oas/3.1/dialect/base")
_VERSION = re.compile(r"3\.(0|1)\.\d+")
_ABSOLUTE_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:")
_RESPONSE_KEY = re.compile(r"[1-5](?:[0-9]{2}|XX)|default")
MAX_REFERENCE_FILES = 128
MAX_REFERENCE_BYTES = 32 * 1024 * 1024


def pointer_escape(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def pointer_unescape(value: str) -> str:
    return value.replace("~1", "/").replace("~0", "~")


def pointer_get(document, pointer: str):
    """RFC 6901 lookup; raises ``LookupError`` when the pointer leads nowhere."""
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise LookupError(f"Invalid JSON pointer: {pointer}")
    value = document
    for raw in pointer[1:].split("/"):
        key = pointer_unescape(raw)
        if isinstance(value, dict):
            if key not in value:
                raise LookupError(f"Token {key!r} does not exist")
            value = value[key]
        elif isinstance(value, list):
            if not (key.isascii() and key.isdigit()) or (len(key) > 1 and key.startswith("0")) \
                    or int(key) >= len(value):
                raise LookupError(f"Token {key!r} does not exist")
            value = value[int(key)]
        else:
            raise LookupError(f"Token {key!r} does not exist")
    return value


def _encode_pointer(pointer: str) -> str:
    # Keep JSON pointers usable as URI fragments (percent-encode "%" and URI delimiters).
    return re.sub(r"[%#?\s\"<>\\^`{|}]", lambda m: "".join(f"%{b:02X}" for b in m.group().encode()), pointer)


# ---------------------------------------------------------------------------
# Schema walks

SCHEMA_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"})
SCHEMA_LISTS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
SCHEMA_SINGLE = frozenset({"items", "additionalItems", "additionalProperties", "contains", "propertyNames",
                           "not", "if", "then", "else", "unevaluatedItems", "unevaluatedProperties",
                           "contentSchema"})
# json-schema-traverse keyword tables, extended for 2020-12 as the original did.
_TRAVERSE_KEYWORDS = frozenset({"additionalItems", "items", "contains", "additionalProperties", "propertyNames",
                                "not", "if", "then", "else", "unevaluatedProperties", "unevaluatedItems",
                                "contentSchema"})
_TRAVERSE_ARRAYS = frozenset({"items", "allOf", "anyOf", "oneOf", "prefixItems"})
_TRAVERSE_PROPS = frozenset({"$defs", "definitions", "properties", "patternProperties", "dependencies",
                             "dependentSchemas"})


def visit_schema(schema, callback) -> None:
    """Pre-order walk over every subschema object (``json-schema-traverse`` semantics)."""
    if not isinstance(schema, dict):
        return
    callback(schema)
    for key, value in list(schema.items()):
        if isinstance(value, list):
            if key in _TRAVERSE_ARRAYS:
                for item in value:
                    visit_schema(item, callback)
        elif key in _TRAVERSE_PROPS:
            if isinstance(value, dict):
                for child in list(value.values()):
                    visit_schema(child, callback)
        elif key in _TRAVERSE_KEYWORDS:
            visit_schema(value, callback)


def is_write_only(schema, document, seen: set[int] | None = None) -> bool:
    """Follow annotation-only, unconditional edges (``$ref`` and ``allOf``) to ``writeOnly: true``."""
    if not isinstance(schema, dict):
        return False
    if schema.get("writeOnly") is True:
        return True
    seen = set() if seen is None else seen
    if id(schema) in seen:
        return False
    seen.add(id(schema))
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        try:
            target = pointer_get(document, unquote(ref[1:]))
        except LookupError:
            target = None  # Validation reports invalid references later.
        if is_write_only(target, document, seen):
            return True
    parts = schema.get("allOf")
    return isinstance(parts, list) and any(is_write_only(part, document, seen) for part in parts)


# ---------------------------------------------------------------------------
# Local bundling


class _Bundler:
    def __init__(self, root_path: str, root):
        self.root_path = os.path.realpath(root_path)
        self.root = root
        self.documents = {self.root_path: root}
        self.files = 0
        self.bytes = 0
        self.imported: dict[tuple[str, str], str] = {}
        self.anchors: dict[int, dict[str, str]] = {}
        self.inlining: list[tuple[str, str]] = []
        self.problems: list[str] = []
        self.seen: set[tuple[int, str, str]] = set()
        self.alive: list = []  # Keeps visited objects alive so their ids stay unique.

    def run(self):
        self.walk(self.root, self.root_path, "object", ())
        if self.problems:
            raise CheckError("Cannot bundle OpenAPI references: " + "; ".join(self.problems[:5]))
        return self.root

    # Walking -----------------------------------------------------------------

    def walk(self, node, file: str, context: str, path: tuple) -> None:
        if not isinstance(node, (dict, list)):
            return
        marker = (id(node), context, file)
        if marker in self.seen:
            return
        self.seen.add(marker)
        self.alive.append(node)
        if isinstance(node, list):
            for item in node:
                self.walk(item, file, context, path)
            return
        if isinstance(node.get("$ref"), str) and self.reference(node, file, context):
            return
        if context == "schema":
            self.walk_schema(node, file)
        elif context == "example":
            self.walk_example(node, file)
        else:
            self.walk_object(node, file, path)

    def walk_object(self, node: dict, file: str, path: tuple) -> None:
        for key, value in list(node.items()):
            if key == "$ref" or key == "example" or key.startswith("x-"):
                continue  # Literal data, never references.
            if key == "examples" and isinstance(value, dict):
                for example in list(value.values()):
                    self.walk(example, file, "example", ())
            elif key == "schema":
                self.walk(value, file, "schema", ())
            elif key == "schemas" and path == ("components",) and isinstance(value, dict):
                for schema in list(value.values()):
                    self.walk(schema, file, "schema", ())
            else:
                self.walk(value, file, "object", (*path, key))

    def walk_example(self, node: dict, file: str) -> None:
        for key, value in list(node.items()):
            if key not in ("$ref", "value") and not key.startswith("x-"):
                self.walk(value, file, "object", ())

    def walk_schema(self, node: dict, file: str) -> None:
        for key, value in list(node.items()):
            if key in SCHEMA_MAPS and isinstance(value, dict):
                for child in list(value.values()):
                    self.walk(child, file, "schema", ())
            elif (key in SCHEMA_LISTS or key == "items") and isinstance(value, list):
                for child in value:
                    self.walk(child, file, "schema", ())
            elif key in SCHEMA_SINGLE and isinstance(value, dict):
                self.walk(value, file, "schema", ())
            elif key == "dependencies" and isinstance(value, dict):
                for child in list(value.values()):
                    if isinstance(child, dict):
                        self.walk(child, file, "schema", ())
            # example, examples, default, enum, const and unknown keywords are literal data.

    # References --------------------------------------------------------------

    def reference(self, node: dict, file: str, context: str) -> bool:
        """Resolve ``node["$ref"]``; return True when the node's content was fully handled."""
        ref = node["$ref"]
        file_part, _, fragment = ref.partition("#")
        try:
            if file_part and _ABSOLUTE_URL.match(file_part) and not file_part.lower().startswith("file:"):
                raise CheckError(f"Remote references are disabled: {ref}")
            target_file = self.locate(file_part, file) if file_part else file
            document = self.load(target_file)
            pointer = self.fragment_pointer(fragment, document)
            target = pointer_get(document, pointer)
        except (CheckError, LookupError, ValueError) as exc:
            self.problems.append(f"Can't resolve $ref {ref}: {exc}")
            return True
        if target_file == self.root_path:
            if file_part or (fragment and not fragment.startswith("/")):
                node["$ref"] = "#" + _encode_pointer(pointer)
            return False
        if context == "schema":
            key = (target_file, pointer)
            name = self.imported.get(key)
            if name is None:
                name = self.import_schema(key, target)
            if name is not None:
                node["$ref"] = "#/components/schemas/" + _encode_pointer(pointer_escape(name))
            return False
        return self.inline(node, target_file, pointer, target, context)

    def import_schema(self, key: tuple[str, str], target) -> str | None:
        components = self.root.setdefault("components", {})
        if not isinstance(components, dict) or not isinstance(components.setdefault("schemas", {}), dict):
            self.problems.append("Cannot add bundled schemas: components.schemas is not an object")
            return None
        schemas = components["schemas"]
        file, pointer = key
        base = pointer_unescape(pointer.rsplit("/", 1)[-1]) if pointer else os.path.splitext(os.path.basename(file))[0]
        base = base or "schema"
        name, counter = base, 2
        while name in schemas and schemas[name] != target:
            name, counter = f"{base}-{counter}", counter + 1
        self.imported[key] = name
        if name not in schemas:
            schemas[name] = copy.deepcopy(target)
            self.walk(schemas[name], file, "schema", ())
        return name

    def inline(self, node: dict, target_file: str, pointer: str, target, context: str) -> bool:
        key = (target_file, pointer)
        if key in self.inlining:
            self.problems.append(f"Circular external reference: {node['$ref']}")
            return True
        if not isinstance(target, dict):
            self.problems.append(f"Can't resolve $ref {node['$ref']}: target is not an object")
            return True
        siblings = {k: v for k, v in node.items() if k != "$ref"}
        node.clear()
        node.update(copy.deepcopy(target))
        node.update(siblings)
        self.inlining.append(key)
        try:
            if isinstance(node.get("$ref"), str) and self.reference(node, target_file, context):
                return True
            if context == "example":
                self.walk_example(node, target_file)
            else:
                self.walk_object(node, target_file, ())
        finally:
            self.inlining.pop()
        return True

    def locate(self, file_part: str, base_file: str) -> str:
        if file_part.lower().startswith("file:"):
            path = url2pathname(urlsplit(file_part).path)
        else:
            path = os.path.join(os.path.dirname(base_file), url2pathname(file_part))
        return os.path.realpath(path)

    def load(self, path: str):
        if path in self.documents:
            return self.documents[path]
        try:
            size = os.stat(path).st_size
        except OSError as exc:
            raise CheckError(f"{exc.strerror or exc}: {path}") from None
        if size > MAX_INPUT_BYTES:
            raise CheckError(f"Reference file exceeds 10 MiB: {path}")
        self.bytes += size
        self.files += 1
        if self.files > MAX_REFERENCE_FILES or self.bytes > MAX_REFERENCE_BYTES:
            raise CheckError("Local reference budget exceeded (128 files / 32 MiB total)")
        self.documents[path] = read_data(path)
        return self.documents[path]

    def fragment_pointer(self, fragment: str, document) -> str:
        fragment = unquote(fragment)
        if fragment == "" or fragment.startswith("/"):
            return fragment
        anchors = self.anchors.get(id(document))
        if anchors is None:
            anchors = self.anchors[id(document)] = _collect_anchors(document)
        if fragment not in anchors:
            raise LookupError(f"anchor {fragment!r} was not found")
        return anchors[fragment]


def _collect_anchors(document) -> dict[str, str]:
    anchors: dict[str, str] = {}
    seen: set[int] = set()
    stack = [(document, "")]
    while stack:
        node, pointer = stack.pop()
        if not isinstance(node, (dict, list)) or id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, list):
            stack.extend((item, f"{pointer}/{index}") for index, item in reversed(list(enumerate(node))))
            continue
        anchor = node.get("$anchor")
        if isinstance(anchor, str):
            anchors.setdefault(anchor, pointer)
        stack.extend((value, f"{pointer}/{pointer_escape(key)}") for key, value in reversed(list(node.items()))
                     if key not in ("example", "examples", "default", "enum", "const") and not key.startswith("x-"))
    return anchors


# ---------------------------------------------------------------------------
# Structure and OpenAPI-object dereferencing


def _check_structure(document: dict, is31: bool) -> None:
    """Minimal structural validation of the parts the checks rely on."""
    problems: list[str] = []
    info = document.get("info")
    if not isinstance(info, dict):
        problems.append("# must have required property 'info'")
    else:
        problems += [f"#/info must have required property '{key}'" for key in ("title", "version")
                     if not isinstance(info.get(key), str)]
    paths = document.get("paths")
    if paths is None:
        if not is31:
            problems.append("# must have required property 'paths'")
        elif not any(key in document for key in ("components", "webhooks")):
            problems.append("# must have one of paths, components or webhooks")
    elif not isinstance(paths, dict):
        problems.append("#/paths must be object")
    else:
        for path, item in paths.items():
            where = f"#/paths/{pointer_escape(path)}"
            if path.startswith("x-"):
                continue
            if not path.startswith("/"):
                problems.append(f"{where}: path keys must begin with /")
            if not isinstance(item, dict):
                problems.append(f"{where} must be object")
                continue
            for method in METHODS:
                if method not in item:
                    continue
                operation = item[method]
                if not isinstance(operation, dict):
                    problems.append(f"{where}/{method} must be object")
                    continue
                responses = operation.get("responses")
                if responses is None:
                    if not is31:
                        problems.append(f"{where}/{method} must have required property 'responses'")
                    continue
                if not isinstance(responses, dict):
                    problems.append(f"{where}/{method}/responses must be object")
                    continue
                for status, response in responses.items():
                    if status.startswith("x-"):
                        continue
                    location = f"{where}/{method}/responses/{pointer_escape(status)}"
                    if not _RESPONSE_KEY.fullmatch(status):
                        problems.append(f"{location}: response keys must be HTTP status codes, ranges or default")
                    if not isinstance(response, dict):
                        problems.append(f"{location} must be object")
                    elif "$ref" not in response and not isinstance(response.get("description"), str):
                        problems.append(f"{location} must have required property 'description'")
    if problems:
        raise CheckError("OpenAPI schema validation failed: " + "; ".join(problems[:5]))


_EXCLUDED = (re.compile(r"/schema(?:/|$)"), re.compile(r"/example(?:/|$)"),
             re.compile(r"/examples/[^/]+/value(?:/|$)"))


def _excluded(path: str) -> bool:
    # Schema refs stay intact for the validator (3.1 $ref siblings are conjunctions);
    # example data stays literal. Only OpenAPI routing and Response Objects are inlined.
    return "/components/schemas" in path or any(pattern.search(path) for pattern in _EXCLUDED)


def _dereference(root: dict) -> dict:
    processed: dict[int, object] = {}  # id -> object, kept alive so ids stay unique

    def resolve(ref_object: dict, ancestors: set[int], chain: set[str]):
        ref = ref_object["$ref"]
        if ref in chain:
            return ref_object  # Circular reference chain: keep it ("circular: ignore").
        try:
            target = pointer_get(root, unquote(ref[1:]))
        except LookupError as exc:
            raise CheckError(f'Error resolving $ref pointer "{ref}": {exc}') from None
        if isinstance(target, dict) and isinstance(target.get("$ref"), str) and target["$ref"].startswith("#"):
            target = resolve(target, ancestors, chain | {ref})
        if id(target) in ancestors:
            return ref_object
        siblings = {k: v for k, v in ref_object.items() if k != "$ref"}
        if siblings and isinstance(target, dict):
            return {**target, **siblings}
        return target

    def crawl(node, path: str, ancestors: set[int]) -> None:
        if not isinstance(node, (dict, list)) or id(node) in processed:
            return
        processed[id(node)] = node
        inner = ancestors | {id(node)}
        entries = list(enumerate(node)) if isinstance(node, list) else list(node.items())
        for key, value in entries:
            key_path = f"{path}/{pointer_escape(str(key))}"
            if _excluded(key_path):
                continue
            if isinstance(value, dict) and isinstance(value.get("$ref"), str) and value["$ref"].startswith("#"):
                resolved = resolve(value, inner, set())
                if resolved is not value:
                    node[key] = resolved
                value = resolved
            crawl(value, key_path, inner)

    crawl(root, "#", set())
    return root


# ---------------------------------------------------------------------------
# OpenAPI 3.0 schema -> JSON Schema draft-04

_STRUCTS = ("allOf", "anyOf", "oneOf", "not", "items", "additionalProperties")
_NOT_SUPPORTED = ("nullable", "discriminator", "readOnly", "writeOnly", "xml", "externalDocs", "example",
                  "deprecated")
_VALID_TYPES = ("integer", "number", "string", "boolean", "object", "array")


def _convert_30(schema: dict) -> dict:
    for struct in _STRUCTS:
        if struct not in schema:
            continue
        value = schema[struct]
        if isinstance(value, list):
            schema[struct] = [_convert_30(item) for item in value if isinstance(item, dict)]
        elif value is None:
            del schema[struct]
        elif isinstance(value, dict):
            schema[struct] = _convert_30(value)
    if "properties" in schema:
        properties = schema["properties"]
        converted = {}
        if isinstance(properties, dict):
            for key, prop in properties.items():
                if isinstance(prop, dict) and prop.get("writeOnly") is not True:
                    converted[key] = _convert_30(prop)
        schema["properties"] = converted
        if isinstance(schema.get("required"), list):
            schema["required"] = [name for name in schema["required"] if name in converted]
            if not schema["required"]:
                del schema["required"]
        if not converted:
            del schema["properties"]
    kind = schema.get("type")
    if "type" in schema and kind not in _VALID_TYPES:
        raise CheckError(f"Type {js_json(kind)} is not a valid type")
    if "type" in schema and schema.get("nullable") is True:
        schema["type"] = [kind, "null"]
        if isinstance(schema.get("enum"), list) and None not in schema["enum"]:
            schema["enum"] = [*schema["enum"], None]
    for key in _NOT_SUPPORTED:
        schema.pop(key, None)
    return schema


def _check_31(node: dict) -> None:
    if node.get("$id") or node.get("$dynamicRef") or node.get("$dynamicAnchor"):
        raise CheckError("Schema $id, $dynamicRef and $dynamicAnchor are not supported in this first version")
    dialect = node.get("$schema")
    if dialect and dialect not in OAS_DIALECTS:
        raise CheckError(f"Unsupported schema dialect: {dialect}")
    if dialect == OAS_DIALECTS[1]:
        node["$schema"] = OAS_DIALECTS[0]


# ---------------------------------------------------------------------------
# Routing

_TEMPLATE = re.compile(r"\{.*?\}")


class Router:
    """Match a request method and URL path to an OpenAPI operation."""

    def __init__(self, definition: dict, api_root: str = "/"):
        self.api_root = api_root or "/"
        self.operations: list[dict] = []
        paths = definition.get("paths") or {}
        for path, item in (paths.items() if isinstance(paths, dict) else ()):
            if not isinstance(item, dict):
                continue
            for method in METHODS:
                if method not in item:
                    continue
                operation = item[method] if isinstance(item[method], dict) else {}
                self.operations.append({
                    **operation, "path": path, "method": method,
                    "parameters": [*(operation.get("parameters") or []), *(item.get("parameters") or [])],
                    "security": operation.get("security") or definition.get("security") or [],
                })

    def _normalize(self, path: str) -> str:
        path = path.strip()
        if path.startswith(self.api_root):
            path = re.sub(f"^{self.api_root}/?", "/", path, count=1)
        return path

    def match_operation(self, method: str, path: str) -> dict | None:
        path = (path or "").strip()
        if not path.startswith("/"):
            path = f"/{path}"
        path = path.split("?")[0]
        method = method.strip().lower()
        if not path.startswith(self.api_root):
            return None
        normalized = self._normalize(path)
        exact = [op for op in self.operations if op["path"] == normalized]
        for op in exact:
            if op["method"] == method:
                return op
        templated = [op for op in self.operations
                     if re.search(f"^{_TEMPLATE.sub('[^/]+', op['path'])}$", normalized)]
        templated.sort(key=lambda op: len(_TEMPLATE.sub("", op["path"])), reverse=True)
        return next((op for op in templated if op["method"] == method), None)


# ---------------------------------------------------------------------------
# Loading


def load_document(schema_path: str | os.PathLike[str], api_root: str = "/"):
    """Return ``(definition, router, prepared, is31)`` for an OpenAPI 3.0.x/3.1.x file."""
    schema_path = os.fspath(schema_path)
    original = read_data(schema_path)
    version = original.get("openapi") if isinstance(original, dict) else None
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise CheckError("Only OpenAPI 3.0.x and 3.1.x are supported")
    is31 = version.startswith("3.1.")
    # Bundle first: recursive references inside external files must be rebased into
    # this document before OpenAPI objects are dereferenced.
    bundled = _Bundler(schema_path, copy.deepcopy(original)).run()
    _check_structure(bundled, is31)
    definition = _dereference(bundled)
    dialect = definition.get("jsonSchemaDialect")
    if is31 and dialect and dialect not in OAS_DIALECTS:
        raise CheckError(f"Unsupported JSON Schema dialect: {dialect}")
    router = Router(definition, api_root)
    prepared = copy.deepcopy(definition)
    converted: dict[int, object] = {}
    outputs: set[int] = set()
    alive: list = []  # Inputs and outputs stay referenced so their ids stay unique.

    def drop_write_only(node: dict) -> None:
        required, properties = node.get("required"), node.get("properties")
        if isinstance(required, list) and required and properties is not None:
            lookup = properties if isinstance(properties, dict) else {}
            node["required"] = [key for key in required if not is_write_only(lookup.get(key), definition)]

    def convert(schema):
        if id(schema) in outputs:
            return schema  # Shared (dereferenced) objects are converted once.
        if id(schema) in converted:
            return converted[id(schema)]
        contextual = copy.deepcopy(schema)
        visit_schema(contextual, drop_write_only)
        if is31:
            visit_schema(contextual, _check_31)
            result = contextual
        else:
            result = _convert_30(contextual) if isinstance(contextual, dict) else contextual
        converted[id(schema)] = result
        outputs.add(id(result))
        alive.extend((schema, result))
        return result

    def content_schemas(item) -> None:
        content = item.get("content") if isinstance(item, dict) else None
        for media in (content.values() if isinstance(content, dict) else ()):
            if isinstance(media, dict) and "schema" in media:
                media["schema"] = convert(media["schema"])

    def response_schemas(response) -> None:
        content_schemas(response)
        headers = response.get("headers") if isinstance(response, dict) else None
        for header in (headers.values() if isinstance(headers, dict) else ()):
            if isinstance(header, dict):
                if "schema" in header:
                    header["schema"] = convert(header["schema"])
                content_schemas(header)

    def parameter_schemas(parameters, with_content: bool) -> None:
        for param in (parameters if isinstance(parameters, list) else ()):
            if isinstance(param, dict):
                if "schema" in param:
                    param["schema"] = convert(param["schema"])
                if with_content:
                    content_schemas(param)

    paths = prepared.get("paths")
    for item in (paths.values() if isinstance(paths, dict) else ()):
        if not isinstance(item, dict):
            continue
        for method in METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue
            responses = operation.get("responses")
            for response in (responses.values() if isinstance(responses, dict) else ()):
                response_schemas(response)
            content_schemas(operation.get("requestBody"))
            parameter_schemas(operation.get("parameters"), True)
        parameter_schemas(item.get("parameters"), False)
    components = prepared.get("components")
    if isinstance(components, dict):
        schemas = components.get("schemas")
        if isinstance(schemas, dict):
            for key in list(schemas):
                schemas[key] = convert(schemas[key])
        for group, handler in (("responses", response_schemas), ("requestBodies", content_schemas)):
            entries = components.get(group)
            for entry in (entries.values() if isinstance(entries, dict) else ()):
                handler(entry)
        parameters = components.get("parameters")
        parameter_schemas(list(parameters.values()) if isinstance(parameters, dict) else None, True)
    return definition, router, prepared, is31
