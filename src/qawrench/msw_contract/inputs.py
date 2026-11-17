"""Bounded UTF-8 file input, YAML 1.2 / JSON parsing, and explicit request cases.

URL handling follows the WHATWG URL rules for HTTP(S) that the original
JavaScript implementation got from ``new URL()``; header handling follows the
Fetch ``Headers`` rules. YAML is parsed with PyYAML, restricted to the YAML 1.2
core schema (no timestamps, ``yes``/``no`` booleans or merge keys) so documents
read exactly as they did with the ``yaml`` npm package.
"""
from __future__ import annotations

from collections import namedtuple
import json
import os
import re
import stat
from urllib.parse import unquote

from ._js import js_json, js_string, utf8_bytes
from .errors import CheckError

MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_CASES_BYTES = 4 * 1024 * 1024
MAX_CASES = 1000
MAX_ALIAS_COUNT = 100
CASE_FIELDS = ("id", "method", "url", "headers", "json", "body")
_METHOD = re.compile(r"(GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS|TRACE)", re.IGNORECASE)
_FORBIDDEN_METHODS = {"CONNECT", "TRACE", "TRACK"}
_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_UNSET = object()


# ---------------------------------------------------------------------------
# Files


def read_utf8(path: str | os.PathLike[str], max_bytes: int = MAX_INPUT_BYTES) -> str:
    """Read a regular UTF-8 file of at most ``max_bytes`` bytes; a leading BOM is dropped."""
    path = os.fspath(path)
    try:
        metadata = os.stat(path)
        if not stat.S_ISREG(metadata.st_mode):
            raise CheckError(f"Input must be a regular file: {path}")
        if metadata.st_size > max_bytes:
            raise CheckError(f"Input exceeds {max_bytes} bytes: {path}")
        chunks: list[bytes] = []
        size = 0
        with open(path, "rb") as stream:
            while chunk := stream.read(65536):
                size += len(chunk)
                if size > max_bytes:
                    raise CheckError(f"Input exceeds {max_bytes} bytes while reading: {path}")
                chunks.append(chunk)
    except OSError as exc:
        raise CheckError(f"{exc.strerror or exc}: {path}") from exc
    try:
        text = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError:
        raise CheckError(f"Input is not valid UTF-8: {path}") from None
    return text[1:] if text.startswith("\ufeff") else text


def read_data(path: str | os.PathLike[str], max_bytes: int = MAX_INPUT_BYTES):
    """Read a JSON or YAML document (UTF-8, unique keys, bounded aliases)."""
    return parse_data(read_utf8(path, max_bytes))


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey("Map keys must be unique")
        result[key] = value
    return result


def _reject_constant(name):
    raise ValueError(f"{name} is not JSON")


def parse_data(text: str):
    """Parse YAML 1.2 (core schema); JSON input takes a faster, equivalent path."""
    if text.lstrip(" \t\r\n")[:1] in ("{", "["):
        try:
            return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        except _DuplicateKey as exc:
            raise CheckError(str(exc)) from None
        except RecursionError:
            raise CheckError("Input nesting is too deep") from None
        except ValueError:
            pass  # Not strict JSON; YAML is a superset and decides.
    import yaml  # optional dependency (extra "openapi")

    loader = _yaml_loader(yaml)(text)
    try:
        return loader.get_single_data()
    except yaml.YAMLError as exc:
        raise CheckError(_yaml_message(exc)) from None
    except RecursionError:
        raise CheckError("Input nesting is too deep") from None
    finally:
        loader.dispose()


def _yaml_message(exc) -> str:
    problem = getattr(exc, "problem", None)
    if not problem:
        return str(exc)
    mark = getattr(exc, "problem_mark", None)
    return problem if mark is None else f"{problem} at line {mark.line + 1}, column {mark.column + 1}"


def _key_string(key) -> str:
    # The yaml npm package stringifies non-string keys of plain JavaScript objects.
    if key is None:
        return ""
    return key if isinstance(key, str) else js_string(key)


_LOADER = None


def _yaml_loader(yaml):
    global _LOADER
    if _LOADER is not None:
        return _LOADER
    from yaml.constructor import ConstructorError
    from yaml.nodes import MappingNode, ScalarNode, SequenceNode

    class AliasLimitError(yaml.YAMLError):
        problem = "Excessive alias count indicates a resource exhaustion attack"

        def __str__(self):
            return self.problem

    class Loader(yaml.SafeLoader):
        def __init__(self, stream):
            super().__init__(stream)
            self._alias_total = 0
            self._alias_uses: dict[str, int] = {}
            self._alias_inner: dict[str, int] = {}

        def compose_node(self, parent, index):
            # Bound alias expansion like the yaml npm package's maxAliasCount (100): every
            # use of an anchor is weighted by the aliases expanded inside that anchor.
            if self.check_event(yaml.events.AliasEvent):
                anchor = self.peek_event().anchor
                if anchor in self.anchors:
                    inner = self._alias_inner.get(anchor, 0)
                    uses = self._alias_uses.get(anchor, 0) + 1
                    self._alias_uses[anchor] = uses
                    if uses * (1 + inner) > MAX_ALIAS_COUNT:
                        raise AliasLimitError()
                    self._alias_total += 1 + inner
                return super().compose_node(parent, index)
            anchor = getattr(self.peek_event(), "anchor", None)
            before = self._alias_total
            node = super().compose_node(parent, index)
            if anchor is not None:
                self._alias_inner[anchor] = self._alias_total - before
            return node

        def construct_mapping(self, node, deep=False):
            if not isinstance(node, MappingNode):
                raise ConstructorError(None, None, f"expected a mapping node, but found {node.id}",
                                       node.start_mark)
            mapping = {}
            for key_node, value_node in node.value:
                key = _key_string(self.construct_object(key_node, deep=True))
                if key in mapping:
                    raise ConstructorError(None, None, "Map keys must be unique", key_node.start_mark)
                mapping[key] = self.construct_object(value_node, deep=deep)
            return mapping

    # YAML 1.2 core schema: reset the YAML 1.1 implicit resolvers of SafeLoader.
    Loader.yaml_implicit_resolvers = {}
    Loader.add_implicit_resolver("tag:yaml.org,2002:bool",
                                 re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))
    Loader.add_implicit_resolver("tag:yaml.org,2002:int",
                                 re.compile(r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$"),
                                 list("-+0123456789"))
    Loader.add_implicit_resolver(
        "tag:yaml.org,2002:float",
        re.compile(r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?"
                   r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"),
        list("-+0123456789."))
    Loader.add_implicit_resolver("tag:yaml.org,2002:null", re.compile(r"^(?:~|null|Null|NULL|)$"),
                                 ["~", "n", "N", ""])

    def construct_int(loader, node):
        value = loader.construct_scalar(node)
        try:
            if value.startswith("0o"):
                return int(value[2:], 8)
            if value.startswith("0x"):
                return int(value[2:], 16)
            return int(value, 10)
        except ValueError:
            return value

    def construct_float(loader, node):
        value = loader.construct_scalar(node)
        lowered = value.lower()
        if lowered in (".inf", "+.inf"):
            return float("inf")
        if lowered == "-.inf":
            return float("-inf")
        if lowered == ".nan":
            return float("nan")
        try:
            return float(value)
        except ValueError:
            return value

    def construct_plain(loader, node):
        # Tags outside the core schema keep their plain structure, as in the yaml npm package.
        if isinstance(node, ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, SequenceNode):
            return loader.construct_sequence(node, deep=True)
        return loader.construct_mapping(node, deep=True)

    Loader.add_constructor("tag:yaml.org,2002:int", construct_int)
    Loader.add_constructor("tag:yaml.org,2002:float", construct_float)
    for tag in ("timestamp", "binary", "set", "omap", "pairs"):
        Loader.add_constructor(f"tag:yaml.org,2002:{tag}", construct_plain)
    Loader.add_constructor(None, construct_plain)
    _LOADER = Loader
    return Loader


# ---------------------------------------------------------------------------
# WHATWG URL subset (HTTP(S) and relative references)

URL = namedtuple("URL", "scheme username password host port path query fragment")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+\-.]*:")
_DEFAULT_PORTS = {"http": "80", "https": "443"}
_PATH_SET = frozenset(' "#<>?`{}')
_QUERY_SET = frozenset(" \"#<>'")
_FRAGMENT_SET = frozenset(' "<>`')
_USERINFO_SET = frozenset(' "#<>?`{}/:;=@[\\]^|')
_FORBIDDEN_HOST = frozenset("\x00\t\n\r #/:<>?@[\\]^|%\x7f")


class InvalidURL(CheckError):
    def __init__(self):
        super().__init__("Invalid URL")


def url_href(url: URL) -> str:
    userinfo = ""
    if url.username or url.password:
        userinfo = url.username + (f":{url.password}" if url.password else "") + "@"
    port = f":{url.port}" if url.port else ""
    query = "" if url.query is None else f"?{url.query}"
    fragment = "" if url.fragment is None else f"#{url.fragment}"
    if url.host is None:  # Non-HTTP URL kept only to report its protocol.
        return f"{url.scheme}:{url.path}"
    return f"{url.scheme}://{userinfo}{url.host}{port}{url.path}{query}{fragment}"


def url_hash(url: URL) -> str:
    return f"#{url.fragment}" if url.fragment else ""


def _encode(text: str, extra: frozenset) -> str:
    out = []
    for char in text:
        if char < " " or char > "~" or char in extra:
            out.append("".join(f"%{byte:02X}" for byte in utf8_bytes(char)))
        else:
            out.append(char)
    return "".join(out)


def _split_tail(tail: str):
    fragment = None
    query = None
    if "#" in tail:
        tail, fragment = tail.split("#", 1)
    if "?" in tail:
        tail, query = tail.split("?", 1)
    return tail, query, fragment


def _normalize_path(path: str) -> str:
    segments = path.split("/")[1:] if path.startswith("/") else path.split("/")
    out: list[str] = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        lowered = segment.lower()
        if lowered in ("..", ".%2e", "%2e.", "%2e%2e"):
            if out:
                out.pop()
            if last:
                out.append("")
        elif lowered in (".", "%2e"):
            if last:
                out.append("")
        else:
            out.append(_encode(segment, _PATH_SET))
    return "/" + "/".join(out)


def _host(raw: str) -> str:
    if raw.startswith("["):
        if not raw.endswith("]") or len(raw) < 3:
            raise InvalidURL()
        return raw.lower()
    host = unquote(raw)
    if not host:
        raise InvalidURL()
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise InvalidURL() from None
    host = host.lower()
    if any(char in _FORBIDDEN_HOST or char < " " for char in host):
        raise InvalidURL()
    return host


def _absolute(scheme: str, rest: str) -> URL:
    end = min((index for index in (rest.find(c) for c in "/?#") if index >= 0), default=len(rest))
    authority, tail = rest[:end], rest[end:]
    userinfo, at, hostport = authority.rpartition("@")
    if not at:
        userinfo = ""
    username, _, password = userinfo.partition(":")
    if hostport.startswith("["):
        close = hostport.find("]")
        if close < 0:
            raise InvalidURL()
        host_text, after = hostport[:close + 1], hostport[close + 1:]
        if after and not after.startswith(":"):
            raise InvalidURL()
        port = after[1:]
    else:
        host_text, _, port = hostport.partition(":")
    if port:
        if not port.isascii() or not port.isdigit() or int(port) > 65535:
            raise InvalidURL()
        port = str(int(port))
        if port == _DEFAULT_PORTS.get(scheme):
            port = ""
    path, query, fragment = _split_tail(tail)
    return URL(scheme, _encode(username, _USERINFO_SET), _encode(password, _USERINFO_SET), _host(host_text),
               port, _normalize_path(path or "/"),
               None if query is None else _encode(query, _QUERY_SET),
               None if fragment is None else _encode(fragment, _FRAGMENT_SET))


def _relative(ref: str, base: URL) -> URL:
    if ref.startswith("//"):
        return _absolute(base.scheme, ref.lstrip("/"))
    path, query, fragment = _split_tail(ref)
    if path.startswith("/"):
        new_path = path
    elif path:
        new_path = base.path[:base.path.rfind("/") + 1] + path
    else:
        new_path = base.path
        if query is None:
            query = base.query
    return URL(base.scheme, base.username, base.password, base.host, base.port, _normalize_path(new_path),
               None if query is None else _encode(query, _QUERY_SET),
               None if fragment is None else _encode(fragment, _FRAGMENT_SET))


def parse_url(value: str, base: str | None = None) -> URL:
    """``new URL(value, base)`` for HTTP(S); other schemes are only identified."""
    text = value.strip("".join(map(chr, range(0x21))))
    text = re.sub(r"[\t\n\r]", "", text)
    base_url = None if base is None else parse_url(base)
    match = _SCHEME.match(text)
    if match:
        scheme = match.group()[:-1].lower()
        rest = text[match.end():]
        if scheme not in _DEFAULT_PORTS:
            return URL(scheme, "", "", None, "", rest, None, None)
        rest = rest.replace("\\", "/")
        if base_url is not None and base_url.scheme == scheme and not rest.startswith("/"):
            return _relative(rest, base_url)
        return _absolute(scheme, rest.lstrip("/"))
    if base_url is None or base_url.host is None:
        raise InvalidURL()
    return _relative(text.replace("\\", "/"), base_url)


def _is_http(url: URL) -> bool:
    return url.scheme in _DEFAULT_PORTS and not url.username and not url.password and not url_hash(url)


def normalize_base_url(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = js_string(value)
    url = parse_url(value)
    if not _is_http(url):
        raise CheckError("base_url must be an HTTP(S) URL without credentials or fragments")
    return url_href(url)


# ---------------------------------------------------------------------------
# Fetch Headers subset


def _header_pairs(init):
    if init is _UNSET:
        return []
    if isinstance(init, dict):
        return list(init.items())
    if isinstance(init, list):
        pairs = []
        for pair in init:
            if not isinstance(pair, (list, tuple)):
                raise CheckError("Headers constructor: expected name/value pair to be an array")
            if len(pair) != 2:
                raise CheckError(f"Headers constructor: expected name/value pair to be length 2, found {len(pair)}.")
            pairs.append(tuple(pair))
        return pairs
    raise CheckError("Headers constructor: Expected init to be an object or an array of pairs.")


def _byte_string(value: str) -> str:
    for index, char in enumerate(value):
        if ord(char) > 255:
            raise CheckError("Cannot convert argument to a ByteString because the character at index "
                             f"{index} has a value of {ord(char)} which is greater than 255.")
    return value


def normalize_headers(init=_UNSET) -> dict[str, str]:
    """``Object.fromEntries(new Headers(init))``: lowercase, sorted, duplicates joined."""
    headers: dict[str, str] = {}
    for raw_name, raw_value in _header_pairs(init):
        name = _byte_string(js_string(raw_name))
        value = _byte_string(js_string(raw_value)).strip(" \t\n\r")
        if not _TOKEN.fullmatch(name):
            raise CheckError(f'Headers.append: "{name}" is an invalid header name.')
        if any(char in value for char in "\x00\r\n"):
            raise CheckError(f'Headers.append: "{value}" is an invalid header value.')
        key = name.lower()
        headers[key] = f"{headers[key]}, {value}" if key in headers else value
    return dict(sorted(headers.items()))


# ---------------------------------------------------------------------------
# Cases


def normalize_cases(data, base_url: str | None = None) -> list[dict]:
    """Validate explicit request cases before any handler is started."""
    if not isinstance(data, list) or not data or len(data) > MAX_CASES:
        raise CheckError(f"Cases must be a non-empty array with at most {MAX_CASES} explicit requests")
    ids: set[str] = set()
    examples = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise CheckError(f"Case {index + 1} must be an object")
        for key in item:
            if key not in CASE_FIELDS:
                raise CheckError(f"Unknown case field: {key}")
        case_id = item.get("id", f"case-{index + 1}")
        if not isinstance(case_id, str) or not case_id or case_id in ids:
            raise CheckError(f"Case IDs must be unique non-empty strings: {js_string(case_id)}")
        ids.add(case_id)
        if not isinstance(item.get("url"), str):
            raise CheckError(f"Case {case_id}: url must be a string")
        url = parse_url(item["url"], base_url)
        if not _is_http(url):
            raise CheckError(f"Case {case_id}: use an HTTP(S) URL without credentials or fragments")
        method = item.get("method", "GET")
        if not isinstance(method, str) or not _METHOD.fullmatch(method):
            raise CheckError(f"Case {case_id}: unsupported method")
        if "body" in item and not isinstance(item["body"], str):
            raise CheckError(f"Case {case_id}: body must be a string; use json for JSON values")
        if "json" in item and "body" in item:
            raise CheckError(f"Case {case_id}: choose json or body")
        headers = normalize_headers(item["headers"] if "headers" in item else _UNSET)
        body = item.get("body")
        if "json" in item:
            try:
                body = js_json(item["json"])
            except (TypeError, ValueError, RecursionError) as exc:
                raise CheckError(f"Case {case_id}: json must be serializable JSON ({exc})") from None
            headers.setdefault("content-type", "application/json")
            headers = dict(sorted(headers.items()))
        if method.upper() in ("GET", "HEAD") and body is not None:
            raise CheckError(f"Case {case_id}: {method} cannot have a body")
        if method.upper() in _FORBIDDEN_METHODS:
            raise CheckError(f"'{method}' HTTP method is unsupported.")
        example = {"id": case_id, "method": method.upper(), "url": url_href(url), "headers": headers}
        if body is not None:
            example["body"] = body
        examples.append(example)
    return examples


__all__ = ["MAX_INPUT_BYTES", "MAX_CASES_BYTES", "normalize_base_url", "normalize_cases",
           "normalize_headers", "parse_data", "parse_url", "read_data", "read_utf8", "url_href"]
