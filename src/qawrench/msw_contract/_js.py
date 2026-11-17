"""JavaScript-compatible text helpers.

Reports were originally produced by JavaScript. These helpers keep strings that
end up in reports (JSON samples, numbers in messages, byte-bounded prefixes)
identical to ``JSON.stringify``, ``String(value)`` and ``TextDecoder`` output.
"""
from __future__ import annotations

import codecs
from decimal import Decimal
import json
import math
import re

_SURROGATE = re.compile("[\ud800-\udfff]")


def js_number(value: int | float) -> str:
    """ECMAScript ``Number.prototype.toString()`` for finite and special values."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"
    sign, digits, exponent = Decimal(repr(value)).as_tuple()
    digits = list(digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    text = "".join(map(str, digits))
    k = len(digits)
    n = exponent + k
    if k <= n <= 21:
        out = text + "0" * (n - k)
    elif 0 < n <= 21:
        out = text[:n] + "." + text[n:]
    elif -6 < n <= 0:
        out = "0." + "0" * (-n) + text
    else:
        power = n - 1
        out = text[0] + ("." + text[1:] if k > 1 else "") + "e" + ("+" if power >= 0 else "-") + str(abs(power))
    return ("-" if sign else "") + out


def js_string(value: object) -> str:
    """``String(value)`` for JSON-like values."""
    if value is None:
        return "null"
    if isinstance(value, str):
        return value
    if isinstance(value, (bool, int, float)):
        return js_number(value)
    if isinstance(value, (list, tuple)):
        return ",".join("" if item is None else js_string(item) for item in value)
    return "[object Object]"


def _quote(text: str) -> str:
    return _SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", json.dumps(text, ensure_ascii=False))


def js_json(value: object) -> str:
    """``JSON.stringify(value)`` without indentation."""
    active: set[int] = set()

    def encode(item: object) -> str:
        if item is None:
            return "null"
        if isinstance(item, str):
            return _quote(item)
        if isinstance(item, bool):
            return "true" if item else "false"
        if isinstance(item, int):
            return str(item)
        if isinstance(item, float):
            return js_number(item) if math.isfinite(item) else "null"
        if isinstance(item, (list, tuple, dict)):
            if id(item) in active:
                raise TypeError("Converting circular structure to JSON")
            active.add(id(item))
            try:
                if isinstance(item, dict):
                    return "{" + ",".join(f"{_quote(js_string(key))}:{encode(child)}"
                                          for key, child in item.items()) + "}"
                return "[" + ",".join(encode(child) for child in item) + "]"
            finally:
                active.discard(id(item))
        raise TypeError(f"Object of type {type(item).__name__} is not JSON serializable")

    return encode(value)


def dumps_report(report: object) -> str:
    """``JSON.stringify(report, null, 2)``: two-space indentation, raw non-ASCII."""
    text = json.dumps(report, indent=2, ensure_ascii=False)
    return _SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)


def utf8_bytes(value: str | bytes) -> bytes:
    """``Buffer.from(value)``: lone surrogates become U+FFFD."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return _SURROGATE.sub("\ufffd", value).encode("utf-8")


def utf8_length(value: str | bytes) -> int:
    return len(utf8_bytes(value))


def utf8_prefix(value: str | bytes, max_bytes: int) -> str:
    """Decode only complete UTF-8 characters within the first ``max_bytes`` bytes."""
    data = utf8_bytes(value)[:max(0, max_bytes)]
    # TextDecoder in streaming mode: drops a leading BOM and withholds a partial character.
    return codecs.getincrementaldecoder("utf-8-sig")(errors="replace").decode(data, final=False)
