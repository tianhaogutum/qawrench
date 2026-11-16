"""Bounded Markdown occurrence reader, without URL or source-path normalization.

Single-line links/definitions in prose, headings, space-indented list/blockquote
containers and bounded lazy paragraph prose. Not a general CommonMark implementation.
Unsupported blocks are skipped. Offsets refer to the original source spelling.
"""

from __future__ import annotations

import html
import re
import string
from collections.abc import Iterator
from dataclasses import dataclass


_ENTITY = re.compile(r"&(?:#[xX][0-9a-fA-F]+|#[0-9]+|[A-Za-z][A-Za-z0-9]+);")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)$")
_CONTAINER = re.compile(r" {0,3}(?:>|[-+*][ \t]|[0-9]{1,9}[.)][ \t])")
_QUOTE = re.compile(r" {0,3}> ?")
_LIST = re.compile(r" {0,3}(?P<marker>[-+*]|[0-9]{1,9}[.)])(?P<space> +|$)")
_BREAK = re.compile(r" {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})")
_HEADING = re.compile(r" {0,3}#{1,6}(?:[ \t]|$)")
_SETEXT = re.compile(r" {0,3}(?:=+|-+)[ \t]*")
_AUTOLINK = re.compile(r"<(?ai:https?://)[^<>\s\x00-\x20\x7f-\x9f]+>")
_TAG_NAME = r"[A-Za-z][A-Za-z0-9-]*"
_ATTRIBUTE = (r"[ \t]+[A-Za-z_:][A-Za-z0-9_.:-]*"
              r"(?:[ \t]*=[ \t]*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^ \t\r\n\"'=<>`]+))?")
_HTML_TAG = re.compile(r"<(?:" + _TAG_NAME + r"(?:" + _ATTRIBUTE + r")*[ \t]*/?"
                       r"|/" + _TAG_NAME + r"[ \t]*)>")


def _autolink_end(text: str, pos: int) -> int | None:
    """Recognize a literal single-line HTTP(S) token, without decoding its URL."""
    match = _AUTOLINK.match(text, pos)
    return match.end() if match else None


def _html_tag_end(text: str, pos: int) -> int | None:
    """Complete single-line open/close tag; not a general HTML parser.

    Attributes are whitespace-separated ASCII names, optionally with a quoted
    or unquoted value. Quotes close literally (no backslash escaping); angles
    and backticks inside quoted values are literal. Unquoted values reject
    whitespace, quotes, equals, angles and backticks. Never span CR/LF.
    """
    match = _HTML_TAG.match(text, pos)
    return match.end() if match else None


def _html_start(line: str) -> bool:
    stripped = line.lstrip()
    return stripped.startswith("<") and _autolink_end(stripped, 0) is None


def _comment_end(text: str, pos: int) -> int | None:
    """Exact <!-- opener and first literal -->, without nesting or CR/LF.

    Empty contents are allowed; quotes and backslashes have no delimiter role.
    This recognizes one bounded token, not general HTML comment grammar.
    """
    if not text.startswith("<!--", pos):
        return None
    close = text.find("-->", pos + 4)
    if close == -1 or any(c in text[pos + 4:close] for c in ("\r", "\n", "<!--")):
        return None
    return close + 3


def _processing_instruction_end(text: str, pos: int) -> int | None:
    """Exact <? through the first literal ?>, with no CR/LF in between.

    Empty contents and further <? openers are literal. Quotes and backslashes
    do not escape the terminator. No target-name or general HTML parsing.
    """
    if not text.startswith("<?", pos):
        return None
    close = text.find("?>", pos + 2)
    if close == -1 or any(c in text[pos + 2:close] for c in ("\r", "\n")):
        return None
    return close + 2


def _cdata_end(text: str, pos: int) -> int | None:
    """Exact <![CDATA[ through the first literal ]]>, with no CR/LF.

    Empty contents and nested openers are literal; quotes and backslashes do
    not escape the terminator. No general HTML or multiline CDATA parsing.
    """
    if not text.startswith("<![CDATA[", pos):
        return None
    close = text.find("]]>", pos + 9)
    if close == -1 or any(c in text[pos + 9:close] for c in ("\r", "\n")):
        return None
    return close + 3


def _declaration_end(text: str, pos: int) -> int | None:
    """Exact <! plus an ASCII uppercase letter, through first literal >.

    The tail after the required letter may be empty, but cannot contain CR/LF.
    Quotes, backslashes and nested openers are literal. No DTD/entity parsing.
    """
    if not (text.startswith("<!", pos) and pos + 2 < len(text)
            and "A" <= text[pos + 2] <= "Z"):
        return None
    close = text.find(">", pos + 3)
    if close == -1 or any(c in text[pos + 3:close] for c in ("\r", "\n")):
        return None
    return close + 1


def _inline_comments_complete(line: str) -> bool:
    """Keep block fallback for unsupported comments outside literal syntax.

    Use the same ordered scan as inline parsing: a marker inside earlier code,
    a tag attribute or a destination/title must not open a comment block.
    """
    return "<!--" not in _mask_code(line, mask_literals=True)


def _lazy_prose(line: str) -> bool:
    """Allow only ordinary paragraph text after omitted container prefixes.

    Reject block-like lines conservatively, including all list markers and HTML
    starts. Valid autolinks are prose. Definition-looking text cannot define links.
    """
    return bool(line.strip()) and not (
        line.startswith("    ") or re.match(r" {0,3}\t", line) or _CONTAINER.match(line)
        or _FENCE.fullmatch(line) or _BREAK.fullmatch(line) or _HEADING.match(line)
        or _SETEXT.fullmatch(line)
        or _html_start(line) or not _inline_comments_complete(line)
    )


@dataclass(frozen=True)
class _Line:
    text: str
    offset: int
    context: tuple[int, ...]


class _Containers:
    """Strip only evidenced prefixes; retain their original code-point offset.

    Each list item/quote has an identity, so sibling blocks cannot share code
    spans or exclusions. Only an active paragraph can retain omitted container
    prefixes, including list indentation and quote markers. Rejected lazy lines
    are skipped until a blank or explicit container. Tabs are never expanded.
    """

    def __init__(self):
        self.stack: list[tuple[str, int, int]] = []
        self.serial = 0
        self.lazy = False
        self.after_blank = False

    def read(self, line: str, *, blocked: bool, paragraph: bool) -> _Line | None:
        after_blank = self.after_blank
        self.after_blank = False
        pos = 0
        matched = []
        for kind, width, identity in self.stack:
            if kind == "quote":
                quote = _QUOTE.match(line, pos)
                if not quote:
                    break
                pos = quote.end()
            elif line[pos:].strip():
                if not line.startswith(" " * width, pos):
                    break
                pos += width
            matched.append((kind, width, identity))
        closed = len(matched) < len(self.stack)
        if (closed and paragraph and not blocked
                and _lazy_prose(line[pos:])):
            # Keep every container identity and paragraph state, but strip only the
            # prefixes actually present. This also preserves cross-line code
            # spans and prevents lazy prose from being promoted to definitions.
            return _Line(line[pos:], pos, tuple(item[2] for item in self.stack))
        sibling_kind = self.stack[len(matched)][0] if closed else None
        self.stack = matched
        opened = False
        # Inside code/HTML, apparent child markers are content. A sibling item
        # (closed=True) ends the old block, and may start a new supported block.
        if closed or not blocked:
            while not _BREAK.fullmatch(line[pos:]):
                quote = _QUOTE.match(line, pos)
                item = _LIST.match(line, pos) if not quote else None
                if not quote and not item:
                    break
                if (item and paragraph and not opened and item['marker'][0].isdigit()
                        and int(item['marker'][:-1]) != 1
                        and sibling_kind != item['marker'][-1]):
                    # An ordered marker other than 1 cannot interrupt prose.
                    # It can continue an existing list of the same marker type.
                    break
                start = pos
                if quote:
                    pos = quote.end()
                    kind = "quote"
                else:
                    # Five or more padding spaces mean one padding space and
                    # indented content, rather than five spaces of list padding.
                    padding = len(item['space'])
                    pos = item.end() if padding <= 4 else item.start('space') + 1
                    kind = item['marker'][-1]
                self.serial += 1
                self.stack.append((kind, pos - start, self.serial))
                opened = True
        self.after_blank = not line.strip()
        if not line[pos:].strip() or opened:
            self.lazy = False
        elif closed:
            # A blank ends lazy eligibility, but a following unindented block
            # may start outside the old list rather than being rejected prose.
            self.lazy = not after_blank
        if self.lazy:
            return None
        return _Line(line[pos:], pos, tuple(item[2] for item in self.stack))


def _decode(text: str) -> str:
    # One pass: an escaped ampersand must not become an entity on a second pass.
    return re.sub(r"\\[" + re.escape(string.punctuation) + r"]|" + _ENTITY.pattern,
                  lambda m: m[0][1:] if m[0].startswith("\\") else html.unescape(m[0]), text)


def _label(text: str) -> str:
    return " ".join(text.split()).casefold() if len(text) <= 999 else ""


def _close(text: str, start: int) -> int | None:
    """Close a simple bracket label. Nested brackets are deliberately unsupported."""
    pos = start + 1
    while pos < len(text):
        if text[pos] == "\\" and pos + 1 < len(text) and text[pos + 1] in string.punctuation:
            pos += 2
        elif text[pos] == "[":
            return None
        elif text[pos] == "]":
            return pos
        else:
            pos += 1
    return None


def _destination(text: str, pos: int) -> tuple[int, int, int] | None:
    """Return raw destination start/end and parser end, excluding angle brackets."""
    start = pos
    angle = pos < len(text) and text[pos] == "<"
    if angle:
        pos += 1
        start = pos
    depth = 0
    while pos < len(text):
        char = text[pos]
        if char == "\\" and pos + 1 < len(text) and text[pos + 1] in string.punctuation:
            pos += 2
            continue
        if angle:
            if char == ">":
                return start, pos, pos + 1
            if char == "<" or ord(char) < 32:
                return None
        else:
            if char.isspace() or char == ")" and depth == 0:
                break
            if ord(char) < 32 or char in "<>":
                return None
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
        pos += 1
    if angle or depth or pos == start:
        return None
    return start, pos, pos


def _tail(text: str, pos: int, *, inline: bool) -> int | None:
    """Validate optional whitespace-separated title and final delimiter."""
    before = pos
    while pos < len(text) and text[pos] in " \t":
        pos += 1
    if pos > before and pos < len(text) and text[pos] in "\"'(":
        end = ")" if text[pos] == "(" else text[pos]
        pos += 1
        while pos < len(text) and text[pos] != end:
            if text[pos] == "\\" and pos + 1 < len(text) and text[pos + 1] in string.punctuation:
                pos += 2
            elif end == ")" and text[pos] == "(":
                return None
            else:
                pos += 1
        if pos == len(text):
            return None
        pos += 1
        while pos < len(text) and text[pos] in " \t":
            pos += 1
    if inline:
        return pos + 1 if pos < len(text) and text[pos] == ")" else None
    return pos if pos == len(text) else None


def _definition(line: str) -> tuple[str, str] | None:
    start = len(line) - len(line.lstrip(" "))
    if start > 3 or start == len(line) or line[start] != "[":
        return None
    close = _close(line, start)
    if close is None or line[close + 1:close + 2] != ":":
        return None
    label = _label(line[start + 1:close])
    if not label:
        return None
    pos = close + 2
    while pos < len(line) and line[pos] in " \t":
        pos += 1
    dest = _destination(line, pos)
    if dest is None or _tail(line, dest[2], inline=False) is None:
        return None
    return label, _decode(line[dest[0]:dest[1]])


def _inline_destination(line: str, pos: int) -> tuple[int, int, int] | None:
    """Validate a single-line destination/title after a closed link/image label."""
    if line[pos:pos + 1] != "(":
        return None
    pos += 1
    while pos < len(line) and line[pos] in " \t":
        pos += 1
    dest = _destination(line, pos)
    tail = _tail(line, dest[2], inline=True) if dest else None
    return (*dest[:2], tail) if tail is not None else None


def _prose(lines: list[str]) -> tuple[list[_Line | None], dict[str, str]]:
    """Conservative block pass; definitions are not occurrences."""
    active: list[_Line | None] = [None] * len(lines)
    references: dict[str, str] = {}
    containers = _Containers()
    context = ()
    fence = None
    skip_block = False
    comment = False
    raw_end = None
    paragraph = False
    for index, original in enumerate(lines):
        source = containers.read(original, blocked=bool(fence or skip_block or comment or raw_end),
                                 paragraph=paragraph)
        if source is None:
            continue
        if source.context != context:
            fence = raw_end = None
            skip_block = comment = paragraph = False
            context = source.context
        line = source.text
        if raw_end:
            if raw_end in line.casefold():
                raw_end = None
            continue
        if comment:
            comment = "-->" not in line
            continue
        if fence:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*", line):
                fence = None
            continue
        match = _FENCE.fullmatch(line)
        if match and (match[1][0] != "`" or "`" not in match[2]):
            fence = match[1]
            paragraph = False
            continue
        if not line.strip():
            skip_block = paragraph = False
            continue
        if "<!--" in line and (line.lstrip().startswith("<!--")
                               or not _inline_comments_complete(line)):
            comment = "-->" not in line.split("<!--", 1)[1]
            paragraph = False
            continue
        if skip_block:
            continue
        if _BREAK.fullmatch(line) or _SETEXT.fullmatch(line):
            paragraph = False
            continue
        raw_tag = re.match(r" {0,3}<(script|pre|style|textarea)(?:[ \t>]|$)", line, re.I)
        terminator = None
        if raw_tag:
            terminator = "</" + raw_tag[1].casefold() + ">"
        elif line.lstrip().startswith("<?"):
            terminator = "?>"
        elif line.lstrip().startswith("<![CDATA["):
            terminator = "]]>"
        elif re.match(r" {0,3}<![A-Z]", line):
            terminator = ">"
        if terminator:
            if terminator not in line.casefold():
                raw_end = terminator
            paragraph = False
            continue
        if (line.startswith("    ") or re.match(r" {0,3}\t", line)
                or _CONTAINER.match(line)
                or _html_start(line)):
            skip_block = True
            paragraph = False
            continue
        definition = _definition(line) if not paragraph else None
        if definition:
            references.setdefault(*definition)
            continue
        active[index] = source
        # ATX headings end their own blocks. Other supported lines form prose.
        paragraph = not bool(_HEADING.match(line))
    return active, references


def _mask_code(text: str, *, mask_literals: bool = False) -> str:
    """Mask code spans in order, treating valid inline tails as literal syntax.

    A code opener in prose or a label keeps precedence over any later apparent
    link. Valid angle autolinks, single-line HTML tags and complete non-nested
    inline comments, processing instructions, CDATA and declarations are atomic,
    including any literal backticks in them.
    Once a simple label closes outside code, its valid destination/title
    (including an image's) cannot open a code span. Malformed tails remain prose.
    The block probe also masks these literal tokens, so only unsupported comment
    openers outside recognized inline syntax remain visible to its fallback.
    """
    chars = list(text)
    pos = 0
    depth = 0
    simple = True
    while pos < len(text):
        if text[pos] == "\\" and pos + 1 < len(text) and text[pos + 1] in string.punctuation:
            pos += 2
        elif text[pos] == "<" and (end := _autolink_end(text, pos)) is not None:
            if mask_literals:
                chars[pos:end] = " " * (end - pos)
            pos = end
        elif text[pos] == "<" and (end := _html_tag_end(text, pos)) is not None:
            if mask_literals:
                chars[pos:end] = " " * (end - pos)
            pos = end
        elif text[pos] == "<" and (end := _comment_end(text, pos)) is not None:
            if mask_literals:
                chars[pos:end] = " " * (end - pos)
            pos = end
        elif text[pos] == "<" and (end := _processing_instruction_end(text, pos)) is not None:
            if mask_literals:
                chars[pos:end] = " " * (end - pos)
            pos = end
        elif text[pos] == "<" and (end := _cdata_end(text, pos)) is not None:
            if mask_literals:
                chars[pos:end] = " " * (end - pos)
            pos = end
        elif text[pos] == "<" and (end := _declaration_end(text, pos)) is not None:
            if mask_literals:
                chars[pos:end] = " " * (end - pos)
            pos = end
        elif text[pos] == "`":
            end = pos
            while end < len(text) and text[end] == "`":
                end += 1
            close = re.search(r"(?<!`)`{" + str(end - pos) + r"}(?!`)", text[end:])
            if close:
                end += close.end()
                for index in range(pos, end):
                    if chars[index] != "\n":
                        chars[index] = " "
                if "\n" in text[pos:end]:
                    depth = 0
            pos = end
        else:
            if text[pos] == "\n":
                depth = 0
            elif text[pos] == "[":
                simple = depth == 0
                depth += 1
            elif text[pos] == "]" and depth:
                depth -= 1
                if depth == 0 and simple:
                    end = text.find("\n", pos)
                    suffix = text[pos + 1:end if end != -1 else len(text)]
                    dest = _inline_destination(suffix, 0)
                    if dest:
                        if mask_literals:
                            chars[pos + 1:pos + 1 + dest[2]] = " " * dest[2]
                        pos += 1 + dest[2]
                        continue
            pos += 1
    return "".join(chars)


def _links(line: str, visible: str, references: dict[str, str]) -> Iterator[tuple[str, int, int]]:
    pos = 0
    while pos < len(line):
        char = visible[pos]
        if char == "\\" and pos + 1 < len(line) and line[pos + 1] in string.punctuation:
            pos += 2
            continue
        if char == "<":
            end = _declaration_end(line, pos)
            if end is not None:
                pos = end
                continue
            end = _cdata_end(line, pos)
            if end is not None:
                pos = end
                continue
            end = _processing_instruction_end(line, pos)
            if end is not None:
                pos = end
                continue
            end = _comment_end(line, pos)
            if end is not None:
                pos = end
                continue
            end = _autolink_end(line, pos)
            if end is not None:
                yield line[pos + 1:end - 1], pos + 1, end - 1
                pos = end
                continue
            end = _html_tag_end(line, pos)
            if end is not None:
                pos = end
                continue
            # Do not inspect HTML attributes or unsupported angle forms as links.
            pos += 1
            quote = None
            while pos < len(line):
                char = visible[pos]
                pos += 1
                if quote:
                    if char == quote:
                        quote = None
                elif char in "\"'":
                    quote = char
                elif char == ">":
                    break
            continue
        image = visible[pos:pos + 2] == "!["
        start = pos + 1 if image else pos
        if visible[start:start + 1] != "[":
            pos += 1
            continue
        close = _close(visible, start)
        if close is None:
            # Nested labels are outside this profile; avoid mapping their internals.
            return
        end = close + 1
        label = _label(line[start + 1:close])
        url = None
        region_start, region_end = start, end
        if line[end:end + 1] == "(":
            dest = _inline_destination(line, end)
            if dest is not None:
                region_start, region_end = dest[:2]
                url = _decode(line[region_start:region_end])
                end = dest[2]
        elif line[end:end + 1] == "[":
            ref_close = _close(line, end)
            if ref_close is not None:
                raw_label = line[end + 1:ref_close]
                label = _label(raw_label) if raw_label else label
                end = ref_close + 1
                region_end = end
                url = references.get(label)
        else:
            url = references.get(label)
        if url and not image and region_end > region_start:
            yield url, region_start, region_end
        pos = end


def link_occurrences(text: str) -> Iterator[tuple[str, dict]]:
    """Yield decoded link destinations/literal autolinks and Unicode regions."""
    lines = text.split("\n")
    active, references = _prose(lines)
    # Reset inline parsing at each unsupported block or paragraph boundary.
    index = 0
    while index < len(lines):
        if not active[index]:
            index += 1
            continue
        end = index + 1
        while end < len(lines) and active[end] and active[end].context == active[index].context:
            # Each ATX heading is its own inline block, even when its container
            # is unchanged. Backticks cannot pair into or out of that block.
            if _HEADING.match(active[end - 1].text) or _HEADING.match(active[end].text):
                break
            end += 1
        visible = _mask_code("\n".join(source.text for source in active[index:end])).split("\n")
        for number in range(index, end):
            source = active[number]
            for url, start, stop in _links(source.text, visible[number - index], references):
                yield url, {"startLine": number + 1, "startColumn": source.offset + start + 1,
                            "endLine": number + 1, "endColumn": source.offset + stop + 1}
        index = end
