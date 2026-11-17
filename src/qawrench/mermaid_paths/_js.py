"""String semantics of the original JavaScript implementation.

Length limits count UTF-16 code units (JavaScript ``String.length``), and
whitespace trimming/collapsing uses JavaScript's ``\\s`` set, which differs from
Python's ``str.isspace`` (for example U+FEFF is whitespace in JavaScript; U+0085
and U+001C..U+001F are not).
"""
from __future__ import annotations

import re

JS_WHITESPACE = ("\t\n\x0b\x0c\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
                 "\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff")
_WS_CLASS = "[" + re.escape(JS_WHITESPACE) + "]"
_WS_RUN = re.compile(_WS_CLASS + "+")
CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def utf16_len(value: str) -> int:
    return len(value) + sum(1 for ch in value if ord(ch) > 0xFFFF)


def js_trim(value: str) -> str:
    return value.strip(JS_WHITESPACE)


def js_collapse(value: str) -> str:
    """``value.replace(/\\s+/gu, ' ').trim()``."""
    return js_trim(_WS_RUN.sub(" ", value))


def is_js_whitespace(ch: str) -> bool:
    return ch in JS_WHITESPACE
