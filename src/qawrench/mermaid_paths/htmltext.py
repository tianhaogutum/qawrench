"""Readable text from the HTML of a Mermaid label, without a DOM library.

The original tool parsed label HTML with parse5 and walked the resulting
fragment. This module tokenizes HTML along the lines of the WHATWG tokenizer
(character references, start/end tags with attributes, comments, bogus comments,
raw-text and RCDATA elements) and applies the same walk to the token stream:
``script``/``style``/``template`` content is skipped; embedded graphics, media,
lists and tables, and hidden/styled/class/ARIA markup are rejected; an image
contributes its non-blank ``alt`` text; block-like elements separate words.

Tree-construction details that only move whitespace (implicit end tags, element
re-parenting) are not reproduced; the caller collapses whitespace afterwards.
"""
from __future__ import annotations

from html.entities import html5 as _HTML5
import re

from ._js import js_trim
from .errors import fail

BREAKS = frozenset(("br", "p", "div", "li", "ul", "ol", "table", "tr", "td", "th", "hr"))
NON_TEXT = frozenset(("svg", "math", "canvas", "video", "audio", "object", "iframe", "ol", "ul",
                      "table", "dl"))
SKIPPED = frozenset(("script", "style", "template"))
VOID = frozenset(("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
                  "track", "wbr", "keygen", "param", "basefont", "bgsound", "frame"))
RAWTEXT = frozenset(("script", "style", "xmp", "iframe", "noembed", "noframes", "noscript"))
RCDATA = frozenset(("title", "textarea"))
IGNORED_TAGS = frozenset(("html", "head", "body", "frameset"))
TABLE_PARTS = frozenset(("caption", "colgroup", "col", "tbody", "thead", "tfoot", "tr", "td", "th"))

# Windows-1252 replacements for numeric references to C1 controls (WHATWG).
_C1 = {
    0x80: 0x20AC, 0x82: 0x201A, 0x83: 0x0192, 0x84: 0x201E, 0x85: 0x2026, 0x86: 0x2020, 0x87: 0x2021,
    0x88: 0x02C6, 0x89: 0x2030, 0x8A: 0x0160, 0x8B: 0x2039, 0x8C: 0x0152, 0x8E: 0x017D, 0x91: 0x2018,
    0x92: 0x2019, 0x93: 0x201C, 0x94: 0x201D, 0x95: 0x2022, 0x96: 0x2013, 0x97: 0x2014, 0x98: 0x02DC,
    0x99: 0x2122, 0x9A: 0x0161, 0x9B: 0x203A, 0x9C: 0x0153, 0x9E: 0x017E, 0x9F: 0x0178,
}
_CHARREF = re.compile(r"&(#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[^\t\n\f <&#;]{1,32};?)")
_ALNUM = re.compile(r"[0-9A-Za-z]")
_WS = "\t\n\f\r "


def _numeric(text: str) -> str:
    digits = text[2:] if text[1] in "xX" else text[1:]
    digits = digits.rstrip(";")
    number = int(digits, 16 if text[1] in "xX" else 10)
    if number == 0 or number > 0x10FFFF or 0xD800 <= number <= 0xDFFF:
        return "\ufffd"
    return chr(_C1.get(number, number))


def decode_references(value: str, attribute: bool = False) -> str:
    """Decode HTML character references as an HTML5 tokenizer does."""
    if "&" not in value:
        return value

    def replace(match: re.Match) -> str:
        body = match.group(1)
        if body[0] == "#":
            return _numeric(body)
        if body in _HTML5:
            return _HTML5[body]
        for length in range(len(body) - 1, 1, -1):
            name = body[:length]
            if name in _HTML5:
                rest = body[length:]
                if attribute and not name.endswith(";") and (rest[:1] == "=" or _ALNUM.match(rest[:1] or " ")):
                    return match.group(0)
                return _HTML5[name] + rest
        return match.group(0)

    return _CHARREF.sub(replace, value)


class _Tokenizer:
    def __init__(self, html: str):
        self.html = html.replace("\r\n", "\n").replace("\r", "\n")
        self.i = 0

    def _skip_ws(self):
        html, n = self.html, len(self.html)
        while self.i < n and html[self.i] in _WS:
            self.i += 1

    def _attributes(self):
        """Attributes up to the end of a tag; None at the end of the input."""
        html, n = self.html, len(self.html)
        attrs: dict[str, str] = {}
        while True:
            self._skip_ws()
            if self.i >= n:
                return None
            ch = html[self.i]
            if ch == ">":
                self.i += 1
                return attrs
            if ch == "/":
                self.i += 1
                if self.i < n and html[self.i] == ">":
                    self.i += 1
                    return attrs
                continue
            start = self.i
            self.i += 1
            while self.i < n and html[self.i] not in _WS + "/>=":
                self.i += 1
            name = html[start:self.i].lower()
            self._skip_ws()
            value = ""
            if self.i < n and html[self.i] == "=":
                self.i += 1
                self._skip_ws()
                if self.i >= n:
                    return None
                quote = html[self.i]
                if quote in "\"'":
                    end = html.find(quote, self.i + 1)
                    if end < 0:
                        return None
                    value = html[self.i + 1:end]
                    self.i = end + 1
                elif quote == ">":
                    value = ""
                else:
                    start_value = self.i
                    while self.i < n and html[self.i] not in _WS + ">":
                        self.i += 1
                    value = html[start_value:self.i]
                value = decode_references(value, attribute=True)
            attrs.setdefault(name, value)

    def tokens(self):
        """Yield ('text', str) | ('start', name, attrs) | ('end', name)."""
        html, n = self.html, len(self.html)
        while self.i < n:
            lt = html.find("<", self.i)
            if lt < 0:
                yield "text", decode_references(html[self.i:])
                return
            if lt > self.i:
                yield "text", decode_references(html[self.i:lt])
            self.i = lt
            following = html[lt + 1:lt + 2]
            if html.startswith("<!--", lt):
                if html.startswith("<!-->", lt):
                    self.i = lt + 5
                elif html.startswith("<!--->", lt):
                    self.i = lt + 6
                else:
                    ends = [e for e in (html.find("-->", lt + 4), html.find("--!>", lt + 4)) if e >= 0]
                    if not ends:
                        return
                    end = min(ends)
                    self.i = end + (3 if html.startswith("-->", end) else 4)
                continue
            if following in ("!", "?"):
                end = html.find(">", lt + 2)
                if end < 0:
                    return
                self.i = end + 1
                continue
            if following == "/":
                after = html[lt + 2:lt + 3]
                if after == ">":
                    self.i = lt + 3
                    continue
                if not after.isascii() or not after.isalpha():
                    end = html.find(">", lt + 2)
                    if end < 0:
                        return
                    self.i = end + 1
                    continue
                self.i = lt + 2
                start = self.i
                while self.i < n and html[self.i] not in _WS + "/>":
                    self.i += 1
                name = html[start:self.i].lower()
                if self._attributes() is None:
                    return
                yield "end", name
                continue
            if following.isascii() and following.isalpha():
                self.i = lt + 1
                start = self.i
                while self.i < n and html[self.i] not in _WS + "/>":
                    self.i += 1
                name = html[start:self.i].lower()
                attrs = self._attributes()
                if attrs is None:
                    return
                yield "start", name, attrs
                if name in RAWTEXT or name in RCDATA or name == "plaintext":
                    content, closed = self._raw(name)
                    if content:
                        yield "text", decode_references(content) if name in RCDATA else content
                    if closed:
                        yield "end", name
                    else:
                        return
                continue
            yield "text", "<"
            self.i = lt + 1

    def _raw(self, name: str) -> tuple[str, bool]:
        """Raw text up to the element's end tag, and whether that end tag was complete."""
        html, n = self.html, len(self.html)
        if name == "plaintext":
            content = html[self.i:]
            self.i = n
            return content, False
        pattern = re.compile(r"</" + re.escape(name) + r"(?=[\t\n\f />])", re.IGNORECASE)
        match = pattern.search(html, self.i)
        if match is None:
            content = html[self.i:]
            self.i = n
            return content, False
        content = html[self.i:match.start()]
        self.i = match.end()
        if self._attributes() is None:
            self.i = n
            return content, False
        return content, True


def text_parts(html: str) -> list[str]:
    """The text pieces the original's parse5 walk collected, in document order."""
    parts: list[str] = []
    open_elements: list[str] = []
    skip_depth = 0
    skip_stack: list[str] = []
    first_start = True
    table_parts_allowed = False
    for token in _Tokenizer(html).tokens():
        kind = token[0]
        if kind == "text":
            if not skip_depth:
                parts.append(token[1])
            continue
        name = token[1]
        if kind == "start":
            if first_start:
                table_parts_allowed = name in TABLE_PARTS
                first_start = False
            if skip_depth:
                if name in SKIPPED and name not in VOID:
                    skip_stack.append(name)
                    skip_depth += 1
                continue
            if name in IGNORED_TAGS or (name in TABLE_PARTS and not table_parts_allowed):
                continue
            if name in SKIPPED:
                skip_stack.append(name)
                skip_depth += 1
                continue
            if name in NON_TEXT:
                fail("NON_TEXT_LABEL", "Embedded graphics, lists, and tables are outside basic text labels; "
                     "provide a plain-text label to retain their meaning.")
            attrs = token[2]
            if any(attr in ("hidden", "style", "class") or attr.startswith("aria-") for attr in attrs):
                fail("NON_TEXT_LABEL", "Hidden, styled, class-based, or ARIA-dependent label markup needs an "
                     "explicit plain-text label.")
            if name == "img":
                alt = attrs.get("alt")
                if alt is None or not js_trim(alt):
                    fail("NON_TEXT_LABEL", "An image in a label needs nonempty alt text.")
                parts.append(alt)
                continue
            if name in BREAKS:
                parts.append(" ")
            if name not in VOID:
                open_elements.append(name)
            continue
        # End tags.
        if skip_depth:
            if skip_stack and skip_stack[-1] == name:
                skip_stack.pop()
                skip_depth -= 1
            continue
        if name == "br":
            parts.append(" ")
            continue
        if name in open_elements:
            while open_elements:
                closed = open_elements.pop()
                if closed in BREAKS:
                    parts.append(" ")
                if closed == name:
                    break
        elif name == "p":
            parts.append(" ")
    return parts
