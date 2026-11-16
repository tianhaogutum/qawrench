"""Offline HAR validation and explicit, conservative request rewrite planning."""
from __future__ import annotations

import base64
import binascii
import copy
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import unquote_plus, urlsplit


class DoctorError(ValueError):
    """An actionable input, policy, collision, or output error."""


def _fail(where: str, message: str):
    raise DoctorError(f"{where}: {message}")


def _object(value, where):
    if not isinstance(value, dict):
        _fail(where, "expected an object")
    return value


def _string(value, where, *, nonempty=False):
    if not isinstance(value, str) or (nonempty and not value):
        _fail(where, "expected " + ("a nonempty string" if nonempty else "a string"))
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _fail(where, "unpaired Unicode surrogate is not supported")
    return value


def _json_tree(value, where="$"):
    """Keep library inputs within the same strict JSON domain as file inputs."""
    if isinstance(value, dict):
        for key, item in value.items():
            _string(key, where + " object key")
            _json_tree(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _json_tree(item, f"{where}[{index}]")
    elif isinstance(value, str):
        _string(value, where)
    elif value is None or type(value) in (bool, int):
        pass
    elif type(value) is float and math.isfinite(value):
        pass
    else:
        _fail(where, "expected finite JSON values")


def _pairs(value, where):
    if not isinstance(value, list):
        _fail(where, "expected an array of name/value objects")
    for index, pair in enumerate(value):
        p = f"{where}[{index}]"
        _object(pair, p)
        _string(pair.get("name"), p + ".name")
        _string(pair.get("value"), p + ".value")


def _number(value, where, minimum=-1, integer=False):
    if type(value) not in ((int,) if integer else (int, float)) or value < minimum:
        _fail(where, f"expected {'integer' if integer else 'number'} >= {minimum}")


_URL = re.compile(r"^(https?://[^/?#]+)([^#]*)(#.*)?$")
_BAD_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")


def _url_parts(url, where):
    _string(url, where, nonempty=True)
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url) or "\\" in url:
        _fail(where, "whitespace, control characters and backslashes are unsupported in URLs")
    if _BAD_ESCAPE.search(url):
        _fail(where, "invalid percent escape in URL")
    match = _URL.fullmatch(url)
    if not match:
        _fail(where, "expected an absolute http:// or https:// URL")
    try:
        parsed = urlsplit(url)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError("missing host or credentials in URL")
        parsed.port  # Validate numeric port and range without changing its spelling.
        authority = parsed.netloc
        if not authority.isascii() or "%" in authority or authority.endswith(":"):
            raise ValueError("use an ASCII/punycode host and a valid port")
        if not re.fullmatch(r"(?:[A-Za-z0-9.\-]+|\[[0-9a-fA-F:.]+\])(?::[0-9]+)?", authority):
            raise ValueError("unsupported host spelling")
    except ValueError as exc:
        _fail(where, str(exc))
    return match.groups(default="")


def _query_key(token, where):
    try:
        return unquote_plus(token.partition("=")[0], encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        _fail(where, "query key is not valid percent-encoded UTF-8")


_MIME_TOKEN = r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
_MIME_PARAMETER = re.compile(
    r'[ \t]*;[ \t]*(' + _MIME_TOKEN + r')[ \t]*=[ \t]*(?:(' + _MIME_TOKEN
    + r')|"((?:[^"\\\r\n]|\\[\x20-\x7e])*)")[ \t]*')
_MAX_MULTIPART_MIME_BYTES = 65536
_MAX_MULTIPART_MIME_PARAMETERS = 128


def _multipart_boundary(mime, where):
    """Read a form-data boundary without normalizing or parsing body bytes."""
    # Find the media type without copying the entire parameter suffix.
    separator = mime.find(";")
    position = separator if separator >= 0 else len(mime)
    media = mime[:position].strip(" \t")
    if media.lower() != "multipart/form-data":
        return None
    # Bound regex input before processing any parameters. The character check
    # also avoids encoding a declaration already known to be too large.
    if (len(mime) > _MAX_MULTIPART_MIME_BYTES
            or len(mime.encode("utf-8")) > _MAX_MULTIPART_MIME_BYTES):
        _fail(where, f"multipart MIME declaration exceeds {_MAX_MULTIPART_MIME_BYTES}-byte UTF-8 limit; "
              "re-record with a smaller multipart MIME declaration")
    boundary = None
    names = set()
    count = 0
    while position < len(mime):
        if count >= _MAX_MULTIPART_MIME_PARAMETERS:
            _fail(where, f"multipart MIME parameter count exceeds {_MAX_MULTIPART_MIME_PARAMETERS}-parameter limit; "
                  "re-record with fewer multipart MIME parameters")
        parameter = _MIME_PARAMETER.match(mime, position)
        if parameter is None:
            _fail(where, "multipart boundary declaration has malformed MIME parameters; "
                  "re-record with well-formed multipart MIME parameters")
        count += 1
        name, token, quoted = parameter.groups()
        # Declaration metadata only: the shared parser also reads part headers.
        # HTAB, printable/Unicode text and printable ASCII escapes remain valid.
        if quoted is not None and re.search(r"[\x00-\x08\x0a-\x1f\x7f]", quoted):
            _fail(where, "multipart MIME quoted parameter contains forbidden control characters; "
                  "re-record with HTAB, printable or UTF-8 quoted values")
        if name.lower() == "boundary":
            if boundary is not None:
                _fail(where, "multipart boundary has duplicate declarations")
            boundary = token if token is not None else re.sub(r"\\(.)", r"\1", quoted)
        elif name.lower() in names:
            _fail(where, "multipart MIME parameter has duplicate declarations; "
                  "re-record with unique multipart MIME parameter names")
        names.add(name.lower())
        position = parameter.end()
    if boundary is None:
        _fail(where, "multipart boundary parameter is required")
    if not 1 <= len(boundary) <= 70:
        _fail(where, "multipart boundary must contain 1 to 70 ASCII characters")
    # RFC 2046 section 5.1.1 bchars; internal spaces require a quoted value.
    if not re.fullmatch(r"[0-9A-Za-z'()+_,./:=? -]*[0-9A-Za-z'()+_,./:=?-]", boundary):
        _fail(where, "multipart boundary contains invalid characters or trailing space")
    return boundary.encode("ascii")


_PART_HEADER = re.compile(_MIME_TOKEN.encode("ascii") + rb':[\t\x20-\x7e\x80-\xff]*')
_MAX_PART_HEADER_BYTES = 65536
_MAX_PART_HEADERS = 128
_MAX_MULTIPART_PARTS = 1024


def _multipart_part_headers(body, start, end, where):
    # Search only a bounded prefix of this part, including room for the entire
    # separator after an exactly-at-limit header block. Never copy file data.
    if body.startswith(b'\r\n', start, end):
        header_end = start  # Empty header block: diagnose the missing disposition.
    else:
        header_end = body.find(b'\r\n\r\n', start, min(end, start + _MAX_PART_HEADER_BYTES + 4))
        if header_end < 0:
            if end - start > _MAX_PART_HEADER_BYTES:
                _fail(where, f"part header block exceeds {_MAX_PART_HEADER_BYTES}-byte limit; "
                      "re-record with smaller part headers")
            _fail(where, "missing CRLF header/body separator; re-record with complete part headers")
    headers = {}
    position, count = start, 0
    while position < header_end:
        count += 1
        if count > _MAX_PART_HEADERS:
            _fail(where, f"part header count exceeds {_MAX_PART_HEADERS}-line limit; "
                  "re-record with fewer part headers")
        line_end = body.find(b'\r\n', position, header_end)
        if line_end < 0:
            line_end = header_end
        line = body[position:line_end]
        position = line_end + 2
        if line.startswith((b' ', b'\t')):
            _fail(where, "folded part header is unsupported; re-record with single-line headers")
        if not _PART_HEADER.fullmatch(line):
            _fail(where, "malformed part header; use token names, colon and CRLF-delimited values")
        name, value = line.split(b':', 1)
        name = name.lower()
        if name in headers:
            _fail(where, "duplicate part header is unsupported; re-record with one line per header name")
        headers[name] = value.strip(b' \t')
    if b'content-disposition' not in headers:
        _fail(where, "missing Content-Disposition; re-record with Content-Disposition: form-data; name=\"field\"")
    # Latin-1 is a lossless view for syntax inspection, not a filename decoder.
    value = headers[b'content-disposition'].decode("latin-1")
    disposition = re.match(_MIME_TOKEN, value)
    if disposition is None or disposition.group().lower() != "form-data":
        _fail(where, "expected form-data Content-Disposition; re-record with form-data and a name parameter")
    position, parameters = disposition.end(), set()
    while position < len(value):
        parameter = _MIME_PARAMETER.match(value, position)
        if parameter is None:
            _fail(where, "malformed Content-Disposition parameters; use token or quoted-string values")
        name = parameter.group(1).lower()
        if name in parameters:
            _fail(where, "duplicate Content-Disposition parameter is unsupported; keep one value per name")
        parameters.add(name)
        position = parameter.end()
    if "name" not in parameters:
        _fail(where, "missing name parameter in Content-Disposition; re-record with name=\"field\"")


def _multipart_framing(body, boundary, where):
    # Inspect decoded bytes only. Preamble/epilogue and transport padding are
    # permitted. Lookahead leaves CRLF available for the following delimiter.
    delimiter = re.compile(rb'(?:\A|(?<=\r\n))--' + re.escape(boundary)
                           + rb'(?P<close>--)?[ \t]*(?=\r\n|\Z)')
    opening, part_index = None, 0
    for match in delimiter.finditer(body):
        if opening is None:
            if match.group("close"):
                break
        else:
            # Opening line's CRLF is framing; the CRLF before the next boundary
            # belongs to that delimiter. Empty file payloads remain valid.
            _multipart_part_headers(body, opening.end() + 2, match.start() - 2,
                                    f"{where}.parts[{part_index}]")
            part_index += 1
            if match.group("close"):
                return
        # A non-closing delimiter starts the next part. Refuse it immediately,
        # before scanning/validating its headers or needing a later delimiter.
        if part_index >= _MAX_MULTIPART_PARTS:
            _fail(f"{where}.parts[{part_index}]",
                  f"part count exceeds {_MAX_MULTIPART_PARTS}-part limit; "
                  "re-record with fewer multipart parts per request")
        opening = match
    if opening is None:
        _fail(where, "missing opening multipart boundary delimiter; re-record with exact embedded text/base64")
    _fail(where, "missing closing multipart boundary delimiter; embedded body may be truncated")


def _missing_file_content(payload, where):
    params = payload.get("params")
    if isinstance(params, list):
        for index, param in enumerate(params):
            if isinstance(param, dict) and "fileName" in param:
                _fail(f"{where}.params[{index}].fileName",
                      "missing file content: file metadata cannot supply exact embedded text/base64; "
                      "re-record with embedded content (fileName is never read from disk)")


def _payload(value, where, *, request=False):
    _object(value, where)
    if "_file" in value or "_sha1" in value:
        _fail(where, "attached _file/_sha1 bodies are unsupported; re-record with embedded content")
    _string(value.get("mimeType"), where + ".mimeType")
    if "text" in value:
        text = _string(value["text"], where + ".text")
    elif request:
        _missing_file_content(value, where)
        _fail(where, "postData requires exact embedded text; params-only bodies cannot be reconstructed")
    elif value.get("size") == 0:
        text = ""
    else:
        _fail(where, "response content requires embedded text (or size: 0 for an empty body)")
    if "size" in value:
        _number(value["size"], where + ".size", integer=True)
    encoding = value.get("encoding")
    if "encoding" in value:
        if encoding != "base64":
            _fail(where + ".encoding", "only absent encoding or base64 is supported")
        try:
            body = base64.b64decode(text, validate=True)
        except (ValueError, binascii.Error):
            _fail(where + ".text", "invalid base64 payload")
    else:
        body = text.encode("utf-8")
    if "params" in value:
        if not isinstance(value["params"], list):
            _fail(where + ".params", "expected an array")
        for index, param in enumerate(value["params"]):
            p = f"{where}.params[{index}]"
            _object(param, p)
            _string(param.get("name"), p + ".name")
            for field in ("value", "fileName", "contentType"):
                if field in param:
                    _string(param[field], p + "." + field)
    if request:
        boundary = _multipart_boundary(value["mimeType"], where + ".mimeType")
        if boundary is not None:
            _multipart_framing(body, boundary, where)
    return body


def _validate_request(req, where, *, require_headers=True):
    _object(req, where)
    _string(req.get("method"), where + ".method", nonempty=True)
    if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", req["method"]):
        _fail(where + ".method", "expected an HTTP method token")
    _, tail, _ = _url_parts(req.get("url"), where + ".url")
    if "?" in tail:
        for token in tail.partition("?")[2].split("&"):
            _query_key(token, where + ".url")
    if require_headers or "headers" in req:
        _pairs(req.get("headers"), where + ".headers")
    if "queryString" in req:
        _pairs(req["queryString"], where + ".queryString")
    if "bodySize" in req:
        _number(req["bodySize"], where + ".bodySize", integer=True)
    if "postData" in req:
        _payload(req["postData"], where + ".postData", request=True)
    elif req.get("bodySize", 0) > 0:
        _fail(where, "positive bodySize without embedded postData is unsupported")
    payload_mime = req.get("postData", {}).get("mimeType", "")
    boundary = _multipart_boundary(payload_mime, where + ".postData.mimeType")
    for index, header in enumerate(req.get("headers", [])):
        if header["name"].lower() != "content-type":
            continue
        location = f"{where}.headers[{index}].value Content-Type"
        declared = _multipart_boundary(header["value"], location)
        if boundary != declared:
            _fail(location, "multipart boundary/media type disagrees with postData.mimeType; "
                  "re-record with consistent embedded content")


def validate_har(har: dict) -> None:
    """Validate the documented embedded HAR 1.2 subset, preserving extensions."""
    _json_tree(har)
    _object(har, "HAR")
    log = _object(har.get("log"), "log")
    if log.get("version") != "1.2":
        _fail("log.version", "only HAR 1.2 is supported")
    creator = _object(log.get("creator"), "log.creator")
    for field in ("name", "version"):
        _string(creator.get(field), "log.creator." + field, nonempty=True)
    if not isinstance(log.get("entries"), list):
        _fail("log.entries", "expected an array")
    for index, entry in enumerate(log["entries"]):
        e = f"log.entries[{index}]"
        _object(entry, e)
        req = _object(entry.get("request"), e + ".request")
        res = _object(entry.get("response"), e + ".response")
        _validate_request(req, e + ".request")
        _number(res.get("status"), e + ".response.status", minimum=100, integer=True)
        if res["status"] > 599:
            _fail(e + ".response.status", "expected HTTP status 100..599; failed recordings are unsupported")
        _pairs(res.get("headers"), e + ".response.headers")
        _payload(res.get("content"), e + ".response.content")
        for label, obj in (("request", req), ("response", res)):
            for field in ("httpVersion", "statusText", "redirectURL", "comment"):
                if field in obj:
                    _string(obj[field], f"{e}.{label}.{field}")
            for field in ("headersSize", "bodySize"):
                if field in obj:
                    _number(obj[field], f"{e}.{label}.{field}", integer=True)
            if "cookies" in obj:
                _pairs(obj["cookies"], f"{e}.{label}.cookies")


def _form_mime(value, where):
    _string(value, where)
    # Deliberately bounded: no legacy charset, duplicate or unknown parameter.
    if not re.fullmatch(r'application/x-www-form-urlencoded(?:[ \t]*;[ \t]*charset[ \t]*=[ \t]*(?:utf-8|"utf-8"))?[ \t]*',
                        value, re.IGNORECASE | re.ASCII):
        _fail(where, "requires application/x-www-form-urlencoded with absent or UTF-8 charset")


def _form_text(payload, where):
    if "_file" in payload or "_sha1" in payload:
        _fail(where, "attached _file/_sha1 bodies are unsupported; re-record with embedded content")
    _missing_file_content(payload, where)
    if "encoding" in payload:
        _fail(where + ".encoding", "params-only forms cannot declare a payload encoding")
    unknown = set(payload) - {"mimeType", "params", "comment"}
    if unknown:
        _fail(where, "unknown fields in params-only form: " + ", ".join(sorted(unknown)))
    _form_mime(payload.get("mimeType"), where + ".mimeType")
    params = payload.get("params")
    _pairs(params, where + ".params")  # Values must be explicit, including empty ones.

    def component(value):
        # WHATWG form encoding: '*' stays literal; '~' does not. No newline
        # normalization of these already-recorded scalar values is performed.
        return "".join("+" if b == 32 else chr(b) if b in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789*-._"
                       else f"%{b:02X}" for b in value.encode("utf-8"))

    fields = []
    for index, param in enumerate(params):
        p = f"{where}.params[{index}]"
        unknown = set(param) - {"name", "value", "comment"}
        if unknown:
            _fail(p, "unsupported form param fields: " + ", ".join(sorted(unknown)))
        fields.append(component(param["name"]) + "=" + component(param["value"]))
    return "&".join(fields)


def embed_form_params(har: dict) -> dict:
    """Explicitly serialize params-only UTF-8 forms into a new embedded HAR.

    This chooses canonical bytes; HAR params cannot recover original wire
    spelling. Existing text/base64 is authoritative and never reserialized.
    Validate the entire result before returning; the input is never mutated.
    """
    _json_tree(har)
    result = copy.deepcopy(har)
    log = _object(_object(result, "HAR").get("log"), "log")
    entries = log.get("entries")
    if not isinstance(entries, list):
        _fail("log.entries", "expected an array")
    for index, entry in enumerate(entries):
        where = f"log.entries[{index}].request"
        req = _object(_object(entry, f"log.entries[{index}]").get("request"), where)
        if "postData" not in req:
            continue
        payload = _object(req["postData"], where + ".postData")
        if "text" in payload:
            continue
        text = _form_text(payload, where + ".postData")
        if "bodySize" in req:
            _number(req["bodySize"], where + ".bodySize", integer=True)
            if req["bodySize"] >= 0 and req["bodySize"] != len(text):
                _fail(where + ".bodySize", "does not equal serialized form byte length")
        _pairs(req.get("headers"), where + ".headers")
        for header in req["headers"]:
            name, value = header["name"].lower(), header["value"].strip(" \t")
            if name == "content-type":
                _form_mime(value, where + ".headers Content-Type")
            if name == "content-encoding" and value.lower() != "identity":
                _fail(where + ".headers Content-Encoding", "only identity is supported for params-only forms")
            if name == "content-length" and (not re.fullmatch(r"[0-9]+", value)
                                             or (value.lstrip("0") or "0") != str(len(text))):
                _fail(where + ".headers Content-Length", "does not equal serialized form byte length")
        payload["text"] = text
    validate_har(result)
    return result


def validate_rules(rules: dict) -> None:
    _json_tree(rules)
    _object(rules, "rules")
    unknown = set(rules) - {"origins", "dropQueryKeys", "dropRequestHeaders"}
    if unknown:
        _fail("rules", "unknown fields: " + ", ".join(sorted(unknown)))
    origins = _object(rules.get("origins", {}), "rules.origins")
    for source, target in origins.items():
        for name, value in (("source", source), ("target", target)):
            origin, tail, fragment = _url_parts(value, f"rules.origins {name}")
            if tail or fragment or origin != value:
                _fail("rules.origins", "origins must contain only scheme://host[:port], without a trailing slash")
        if target in origins and origins[target] != target:
            _fail("rules.origins", "chained/cyclic mappings are unsupported; use terminal targets for idempotence")
    keys = rules.get("dropQueryKeys", [])
    if not isinstance(keys, list):
        _fail("rules.dropQueryKeys", "expected an array of explicitly named query keys")
    for key in keys:
        _string(key, "rules.dropQueryKeys[]", nonempty=True)
    if len(set(keys)) != len(keys):
        _fail("rules.dropQueryKeys", "duplicate names are not allowed")
    headers = rules.get("dropRequestHeaders", [])
    if not isinstance(headers, list):
        _fail("rules.dropRequestHeaders", "expected an array of explicitly named request headers")
    for index, name in enumerate(headers):
        where = f"rules.dropRequestHeaders[{index}]"
        _string(name, where, nonempty=True)
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            _fail(where, "expected an ASCII HTTP header-name token; no whitespace or colon")
    if len({name.lower() for name in headers}) != len(headers):
        _fail("rules.dropRequestHeaders", "duplicate case-insensitive names are not allowed")


def _rewrite(url, rules):
    origin, tail, fragment = _url_parts(url, "request.url")
    target = rules.get("origins", {}).get(origin, origin)
    reasons = []
    if target != origin:
        reasons.append({"kind": "origin", "before": origin, "after": target})
    dropped = []
    path, sep, query = tail.partition("?")
    if sep:
        retained = []
        for index, token in enumerate(query.split("&")):
            name = _query_key(token, "request.url")
            if name in rules.get("dropQueryKeys", []):
                dropped.append({"tokenIndex": index, "name": name, "rawToken": token})
            else:
                retained.append(token)
        if dropped:
            # Keep even empty retained tokens and their order; remove '?' only
            # when no tokens remain, so an existing empty query is untouched.
            tail = path + ("?" + "&".join(retained) if retained else "")
            reasons.append({"kind": "dropQueryKeys", "tokens": dropped})
    return target + tail + fragment, reasons


def request_key(request: dict) -> tuple[str, str, bytes]:
    """Exact URL/method/body bytes. Validate the containing HAR/inventory first."""
    body = _payload(request["postData"], "request.postData", request=True) if "postData" in request else b""
    return request["url"], request["method"], body


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("ascii")).hexdigest()


def validate_inventory(inventory: dict) -> None:
    """Validate explicit target requests; no inferred or wildcard matching."""
    _json_tree(inventory)
    _object(inventory, "inventory")
    unknown = set(inventory) - {"schemaVersion", "requests"}
    if unknown:
        _fail("inventory", "unknown fields: " + ", ".join(sorted(unknown)))
    if type(inventory.get("schemaVersion")) is not int or inventory["schemaVersion"] != 1:
        _fail("inventory.schemaVersion", "expected integer 1")
    requests = inventory.get("requests")
    if not isinstance(requests, list) or not requests:
        _fail("inventory.requests", "expected a nonempty array of explicit requests")
    identifiers = set()
    for index, req in enumerate(requests):
        where = f"inventory.requests[{index}]"
        _object(req, where)
        unknown = set(req) - {"id", "url", "method", "headers", "postData", "bodySize"}
        if unknown:
            _fail(where, "unknown fields: " + ", ".join(sorted(unknown)))
        identifier = _string(req.get("id"), where + ".id", nonempty=True)
        if identifier in identifiers:
            _fail(where + ".id", f"duplicate request id {identifier!r}")
        identifiers.add(identifier)
        _validate_request(req, where, require_headers=False)


def export_inventory(har: dict) -> dict:
    """Copy the first request for each exact key into a version-1 inventory.

    IDs identify the first original HAR entry. Embedded postData is copied
    verbatim; no body is inferred from metadata and no URL is rewritten.
    """
    validate_har(har)
    if not har["log"]["entries"]:
        _fail("log.entries", "cannot export an empty HAR; version-1 inventories require "
              "a nonempty requests array; supply a HAR with at least one embedded request")
    requests, seen = [], set()
    for index, entry in enumerate(har["log"]["entries"]):
        req = entry["request"]
        key = request_key(req)
        if key in seen:
            continue
        seen.add(key)
        exported = {"id": f"/log/entries/{index}", "url": req["url"], "method": req["method"]}
        if "postData" in req:
            exported["postData"] = copy.deepcopy(req["postData"])
        requests.append(exported)
    inventory = {"schemaVersion": 1, "requests": requests}
    validate_inventory(inventory)
    return inventory


def _key_json(key):
    url, method, body = key
    return {"url": url, "method": method, "bodyBase64": base64.b64encode(body).decode("ascii")}


def _preflight(har, inventory, rules, groups):
    """Inputs and projected fixture groups have already been validated."""
    by_url = {}
    for key, indexes in groups.items():
        by_url.setdefault(key[0], []).append((key, indexes))
    results = []
    counts = {"matched": 0, "missing": 0, "ambiguous": 0}
    for index, req in enumerate(inventory["requests"]):
        key = request_key(req)  # Inventory is the target; never rewrite it.
        indexes = groups.get(key, [])
        status = "missing" if not indexes else "matched" if len(indexes) == 1 else "ambiguous"
        counts[status] += 1
        near_matches = []
        if status == "missing":
            for candidate, candidate_indexes in by_url.get(key[0], []):
                differing = [field for position, field in ((1, "method"), (2, "body"))
                             if candidate[position] != key[position]]
                near_matches.extend({"entryIndex": i, "entryId": f"/log/entries/{i}",
                                     "differingFields": list(differing)} for i in candidate_indexes)
            near_matches.sort(key=lambda candidate: candidate["entryIndex"])
        results.append({"requestIndex": index, "requestId": req["id"], "key": _key_json(key),
                        "status": status, "entryIndexes": list(indexes),
                        "entryIds": [f"/log/entries/{i}" for i in indexes],
                        "nearMatches": near_matches})
    return {"schemaVersion": 1, "tool": "har-replay-doctor", "matchModel": "exact-url-method-body-v1",
            "harSHA256": _digest(har), "inventorySHA256": _digest(inventory),
            "rules": copy.deepcopy(rules), "passed": counts["missing"] == counts["ambiguous"] == 0,
            "summary": {"requests": len(results), "matchedRequests": counts["matched"],
                        "missingRequests": counts["missing"], "ambiguousRequests": counts["ambiguous"]},
            "requests": results}


def build_preflight(har: dict, inventory: dict, rules: dict | None = None) -> dict:
    """Compare target requests with saved/projected fixture keys, without writes."""
    if rules is None:
        rules = {}
    validate_har(har)
    validate_rules(rules)
    validate_inventory(inventory)
    groups = {}
    for index, entry in enumerate(har["log"]["entries"]):
        url, method, body = request_key(entry["request"])
        after, _ = _rewrite(url, rules)
        groups.setdefault((after, method, body), []).append(index)
    return _preflight(har, inventory, rules, groups)


def select_requests(har: dict, inventory: dict) -> dict:
    """Copy complete uniquely selected entries in capture order, without rewrites.

    Preflight validates every entry and selector before matching. Repeated
    selectors do not consume matches or duplicate entries. Keep all document
    metadata, including pages that may no longer have a selected entry.
    """
    selected = _selected_request_indexes(har, inventory)
    result = copy.deepcopy(har)
    result["log"]["entries"] = [entry for index, entry in enumerate(result["log"]["entries"])
                                if index in selected]
    return result


def _selected_request_indexes(har, inventory):
    """Fully validate and uniquely match before filtering JSON or ZIP entries."""
    report = build_preflight(har, inventory)
    selected = set()
    for match in report["requests"]:
        if match["status"] != "matched":
            _fail(f"inventory.requests[{match['requestIndex']}]",
                  f"{match['status']} exact request match ({len(match['entryIndexes'])} entries); "
                  "selection requires exactly one entry per request; inspect preflight diagnostics")
        selected.add(match["entryIndexes"][0])
    return selected


def build_plan(har: dict, rules: dict, *, inventory: dict | None = None) -> dict:
    """Analyze all entries without modifying either argument or writing files."""
    validate_har(har)
    validate_rules(rules)
    if inventory is not None:
        validate_inventory(inventory)
    changes, groups = [], {}
    drop_headers = {name.lower() for name in rules.get("dropRequestHeaders", [])}
    header_totals = Counter()
    for index, entry in enumerate(har["log"]["entries"]):
        req = entry["request"]
        after, reasons = _rewrite(req["url"], rules)
        removed = [i for i, p in enumerate(req.get("queryString", [])) if p["name"] in rules.get("dropQueryKeys", [])]
        if removed:
            reasons.append({"kind": "queryStringMetadata", "removedIndexes": removed})
        removed_headers = [i for i, row in enumerate(req["headers"]) if _selected_header(row, drop_headers)]
        if removed_headers:
            counts = Counter(req["headers"][i]["name"].lower() for i in removed_headers)
            header_totals.update(counts)
            reasons.append({"kind": "dropRequestHeaders", "removedIndexes": removed_headers,
                            "removedHeaders": _header_counts(counts)})
        changes.append({"entryIndex": index, "beforeURL": req["url"], "afterURL": after,
                        "changed": after != req["url"] or bool(removed) or bool(removed_headers), "reasons": reasons})
        _, method, body = request_key(req)
        groups.setdefault((after, method, body), []).append(index)
    collisions = []
    for (url, method, body), indexes in groups.items():
        if len(indexes) < 2:
            continue
        responses = [har["log"]["entries"][i]["response"] for i in indexes]
        # Compare serialized values themselves, not hashes, for fail-closed decisions.
        conflict = len({_canonical(response) for response in responses}) > 1
        fields = set().union(*(response.keys() for response in responses))
        different = sorted(field for field in fields
                           if len({_canonical([field in r, r.get(field)]) for r in responses}) > 1)
        collisions.append({"key": _key_json((url, method, body)),
                           "entryIndexes": indexes, "conflict": conflict, "differingResponseFields": different,
                           "responseSHA256": [_digest(r) for r in responses]})
    conflicts = sum(group["conflict"] for group in collisions)
    plan = {"schemaVersion": 1, "tool": "har-replay-doctor", "harSHA256": _digest(har),
            "rules": copy.deepcopy(rules), "safeToApply": conflicts == 0,
            "summary": {"entries": len(changes), "changedEntries": sum(c["changed"] for c in changes),
                        "collisionGroups": len(collisions), "conflictingGroups": conflicts},
            "entries": changes, "collisionGroups": collisions}
    if "dropRequestHeaders" in rules:
        plan["summary"].update(removedRequestHeaderRows=sum(header_totals.values()),
                               removedRequestHeaders=_header_counts(header_totals))
    if inventory is not None:
        plan["requestPreflight"] = _preflight(har, inventory, rules, groups)
        plan["safeToApply"] = plan["safeToApply"] and plan["requestPreflight"]["passed"]
    return plan


def _header_counts(counts):
    """Name/count diagnostics only: never copy header values or extensions."""
    return [{"name": name, "count": counts[name]} for name in sorted(counts)]


def _selected_header(row, names):
    # Preserve opaque non-ASCII names accepted by the existing HAR validator;
    # Unicode lowercasing (e.g. Kelvin sign -> k) is not HTTP case matching.
    return row["name"].isascii() and row["name"].lower() in names


def rewrite_har(har: dict, rules: dict, *, inventory: dict | None = None) -> tuple[dict, dict]:
    """Revalidate and recompute a plan, then return a copied HAR or fail closed."""
    plan = build_plan(har, rules, inventory=inventory)
    if "requestPreflight" in plan and not plan["requestPreflight"]["passed"]:
        failures = [{"requestId": r["requestId"], "status": r["status"], "entryIds": r["entryIds"],
                     "nearMatches": r["nearMatches"]}
                    for r in plan["requestPreflight"]["requests"] if r["status"] != "matched"]
        raise DoctorError(f"request preflight failed: {failures}; inspect preflight or plan diagnostics")
    if not plan["safeToApply"]:
        indexes = [g["entryIndexes"] for g in plan["collisionGroups"] if g["conflict"]]
        raise DoctorError(f"conflicting rewritten request keys at entry groups {indexes}; inspect a plan and narrow the rules")
    result = copy.deepcopy(har)
    drop_headers = {name.lower() for name in rules.get("dropRequestHeaders", [])}
    for change, entry in zip(plan["entries"], result["log"]["entries"]):
        entry["request"]["url"] = change["afterURL"]
        entry["request"]["headers"] = [row for row in entry["request"]["headers"]
                                        if not _selected_header(row, drop_headers)]
        if "queryString" in entry["request"]:
            entry["request"]["queryString"] = [p for p in entry["request"]["queryString"]
                                               if p["name"] not in rules.get("dropQueryKeys", [])]
    return result, plan


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DoctorError(f"duplicate JSON object key {key!r}; refusing ambiguous input")
        result[key] = value
    return result


def load_json(path: str | Path):
    try:
        with Path(path).open("r", encoding="utf-8-sig") as stream:
            value = json.load(stream, object_pairs_hook=_unique_object,
                              parse_constant=lambda c: _fail(str(path), f"nonfinite JSON number {c}"))
        _json_tree(value)
        return value
    except DoctorError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise DoctorError(f"cannot read JSON {path}: {exc}") from exc


def atomic_write_file(output: str | Path, write, *, protected=()) -> None:
    """Publish a complete local file atomically, refusing every existing target.

    A same-directory temporary file plus hard-link publication gives atomic
    no-clobber semantics on Linux, including racing writers and symlink targets.
    """
    target = Path(output)
    try:
        for source in protected:
            if target.resolve() == Path(source).resolve():
                raise DoctorError(f"output {output} must differ from input/rules {source}")
        if os.path.lexists(target):
            raise DoctorError(f"output already exists: {output}; choose a new path")
    except DoctorError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise DoctorError(f"invalid output path {output}: {exc}") from exc
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w+b", dir=target.parent,
                                         prefix=f".{target.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    except (OSError, ValueError) as exc:
        raise DoctorError(f"cannot write {output}: {exc}; choose a new writable local path") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_write_json(value, output: str | Path, *, protected=()) -> None:
    # Serialize before filesystem changes. Keep the existing JSON publication policy.
    try:
        data = (json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise DoctorError(f"cannot write {output}: {exc}; choose a new writable local path") from exc
    atomic_write_file(output, lambda stream: stream.write(data), protected=protected)
