"""A standard-library port of the parts of Marked 18 (GFM mode) the tool relies on.

Block level follows Marked's own rules and tokenizer algorithms (blank lines,
indented and fenced code, ATX/setext headings, thematic breaks, block quotes with
lazy continuation, lists, HTML blocks, link definitions, GFM tables, paragraphs),
because Mermaid code-fence extraction depends on exactly which fences Marked
finds, including fences nested in lists and block quotes.

Inline level is simplified: it is used only to turn Markdown labels into HTML
whose *text* is then extracted. Code spans, backslash escapes, raw inline HTML,
autolinks, links, images, hard breaks, emphasis (CommonMark delimiter rules) and
GFM strikethrough are handled; reference links and GFM bare-URL autolinks are
left as text (their text is unchanged anyway).
"""
from __future__ import annotations

import re
import unicodedata

from ._js import JS_WHITESPACE


def _py(src: str) -> str:
    """Translate a JavaScript (non-multiline) regex source to Python syntax."""
    out = []
    i = 0
    in_class = False
    while i < len(src):
        ch = src[i]
        if ch == "\\":
            out.append(src[i:i + 2])
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
            out.append(ch)
        elif ch == "[":
            in_class = True
            out.append(ch)
        elif ch == "$":
            out.append(r"\Z")
        elif ch == ".":
            out.append("[^\\n\\r\\u2028\\u2029]")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


class _Edit:
    """Marked's ``edit()`` helper: textual substitution into a regex source."""

    def __init__(self, src: str):
        self.src = src

    def replace(self, name: str, value: str, every: bool = False) -> "_Edit":
        value = re.sub(r"(^|[^\[])\^", r"\1", value)
        self.src = self.src.replace(name, value) if every else self.src.replace(name, value, 1)
        return self

    def compile(self, flags: int = 0) -> re.Pattern:
        return re.compile(_py(self.src), flags)


TAGS = ("address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|"
        "dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|head|header|"
        "hr|html|iframe|legend|li|link|main|menu|menuitem|meta|nav|noframes|ol|optgroup|option|p|param|"
        "search|section|summary|table|tbody|td|tfoot|th|thead|title|tr|track|ul")
_HR_SRC = r"^ {0,3}((?:-[\t ]*){3,}|(?:_[ \t]*){3,}|(?:\*[ \t]*){3,})(?:\n+|$)"
_BULL = r" {0,3}(?:[*+-]|\d{1,9}[.)])"
_HTML_START = (r" {0,3}(?:</?(?:tag)(?: +|\n|/?>)|<(?:(?:script|pre|style|textarea)(?:[\s>]|$)|!--)|<\?"
               r"|<![A-Za-z]|<!\[CDATA\[)")
_FENCE_START = r" {0,3}(?:`{3,}(?=[^`\n]*(?:\n|$))|~~~)[^\n]*(?:\n|$)"
_PARAGRAPH_SRC = r"^([^\n]+(?:\n(?!hr|heading|lheading|blockquote|fences|list|html|table|[ \t]+\n)[^\n]+)*)"

NEWLINE = re.compile(_py(r"^(?:[ \t]*(?:\n|$))+"))
CODE = re.compile(_py(r"^((?: {4}| {0,3}\t)[^\n]+(?:\n(?:[ \t]*(?:\n|$))*)?)+"))
FENCES = re.compile(_py(r"^ {0,3}(`{3,}(?=[^`\n]*(?:\n|$))|~{3,})([^\n]*)(?:\n|$)(?:|([\s\S]*?)(?:\n|$))"
                        r"(?: {0,3}(?:(?=`)\1`*|(?=~)\1~*) *(?=\n|$)|$)"))
HR = re.compile(_py(_HR_SRC))
HEADING = re.compile(_py(r"^ {0,3}(#{1,6})(?=\s|$)(.*)(?:\n+|$)"))
LIST = _Edit(r"^(bull)([ \t][^\n]*?)?(?:\n|$)").replace("bull", _BULL, every=True).compile()
HTML = (_Edit(r"^ {0,3}(?:<(script|pre|style|textarea)[\s>][\s\S]*?(?:</\1>[^\n]*\n*|$)"
              r"|comment[^\n]*(\n+|$)|<\?[\s\S]*?(?:\?>[^\n]*\n*|$)|<![A-Z][\s\S]*?(?:>[^\n]*\n*|$)"
              r"|<!\[CDATA\[[\s\S]*?(?:\]\]>[^\n]*\n*|$)|</?(tag)(?:[ \t]+|\n|/?>|$)[\s\S]*?(?:(?:\n[ \t]*)+\n|$)"
              r"|<(?!(?:script|pre|style|textarea)(?![a-z0-9-]))([a-z][a-z0-9-]*)(?:attribute)*? */?>"
              r"(?=[ \t]*(?:\n|$))[\s\S]*?(?:(?:\n[ \t]*)+\n|$)"
              r"|</[a-z][a-z0-9-]*\s*>(?=[ \t]*(?:\n|$))[\s\S]*?(?:(?:\n[ \t]*)+\n|$))")
        .replace("comment", r"<!--(?:-?>|[\s\S]*?(?:-->|$))").replace("tag", TAGS)
        .replace("attribute", r""" +[a-zA-Z:_][\w.:-]*(?: *= *"[^"\n]*"| *= *'[^'\n]*'| *= *[^\s"'=<>`]+)?""")
        .compile(re.IGNORECASE))
DEF = (_Edit(r"^ {0,3}\[(label)\]: *(?:\n[ \t]*)?([^<\s][^\s]*|<.*?>)(?:(?: +(?:\n[ \t]*)?| *\n[ \t]*)(title))? *(?:\n+|$)")
       .replace("label", r"(?!\s*\])(?:\\[\s\S]|[^\[\]\\\n]|\n(?![ \t]*\n))+")
       .replace("title", r"""(?:"(?:\\[\s\S]|[^"\\])*"|'(?:\\[\s\S]|[^'\\\n]|\n(?!\n))*'|\((?:\\[\s\S]|[^()\\])*\))""")
       .compile())
_TABLE_SRC = (_Edit(r"^ *([^\n ].*)\n {0,3}((?:\| *)?:?-+:? *(?:\| *:?-+:? *)*(?:\| *)?)"
                    r"(?:\n((?:(?! *\n|hr|heading|blockquote|code|fences|list|html).*(?:\n|$))*)\n*|$)")
              .replace("hr", _HR_SRC).replace("heading", r" {0,3}#{1,6}(?:\s|$)").replace("blockquote", r" {0,3}>")
              .replace("code", r"(?: {4}| {0,3}\t)[^\n]").replace("fences", _FENCE_START)
              .replace("list", r" {0,3}(?:[*+-]|1[.)])[ \t]").replace("html", _HTML_START).replace("tag", TAGS)).src
TABLE = re.compile(_py(_TABLE_SRC))
LHEADING = (_Edit(r"^(?!bull |blockCode|fences|blockquote|heading|html|table)((?:.|\n(?!\s*?\n|bull |fences|"
                  r"blockquote|heading|hr|html|table))+?)\n {0,3}(=+|-+)[ \t]*(?:\n+|$)")
            .replace("bull", _BULL, every=True).replace("blockCode", r"(?: {4}| {0,3}\t)", every=True)
            .replace("fences", r" {0,3}(?:`{3,}|~{3,})", every=True).replace("blockquote", r" {0,3}>", every=True)
            .replace("heading", r" {0,3}#{1,6}(?:\s|$)", every=True)
            .replace("hr", r" {0,3}(?:(?:-[\t ]*){3,}|(?:_[ \t]*){3,}|(?:\*[ \t]*){3,})(?:\n+|$)", every=True)
            .replace("html", r" {0,3}<[^\n>]+>\n", every=True)
            .replace("table", r" {0,3}\|?(?:[:\- ]*\|)+[\:\- ]*\n", every=True).compile())
PARAGRAPH = (_Edit(_PARAGRAPH_SRC).replace("hr", _HR_SRC).replace("heading", r" {0,3}#{1,6}(?:\s|$)")
             .replace("|lheading", "").replace("table", _TABLE_SRC).replace("blockquote", r" {0,3}>")
             .replace("fences", _FENCE_START).replace("list", r" {0,3}(?:[*+-]|1[.)])[ \t]+[^ \t\n]")
             .replace("html", _HTML_START).replace("tag", TAGS).compile())
_BQ_PARAGRAPH = (_Edit(_PARAGRAPH_SRC).replace("hr", _HR_SRC).replace("heading", r" {0,3}#{1,6}(?:\s|$)")
                 .replace("|lheading", "").replace("|table", "").replace("blockquote", r" {0,3}>")
                 .replace("fences", _FENCE_START).replace("list", r" {0,3}(?:[*+-]|\d{1,9}[.)])(?:[ \t]|\n|$)")
                 .replace("html", _HTML_START).replace("tag", TAGS)).src
BLOCKQUOTE = (_Edit(r"^( {0,3}> ?(?:(?![ \t]*(?:\n|$))(paragraph)|[^\n]*)(?:\n|$))+")
              .replace("paragraph", _BQ_PARAGRAPH).compile())
TEXT = re.compile(_py(r"^[^\n]+"))

BLOCKQUOTE_START = re.compile(r"^ {0,3}>")
BQ_MARKER_WS = re.compile(r"^( {0,3}>)([ \t]+)", re.M)
BQ_SETEXT = re.compile(_py(r"\n {0,3}((?:=+|-+)[ \t]*)(?=\n|$)"))
BQ_SETEXT2 = re.compile(r"^ {0,3}>[ \t]?", re.M)
CODE_REMOVE_INDENT = re.compile(r"^(?: {0,3}\t| {1,4})", re.M)
LIST_INTERRUPTS = re.compile(r"^ {0,3}(?:[*+-]|1[.)])[ \t]+[^ \t\n]")
NESTED_CONTAINER_MARKERS = re.compile(r"^(?: {0,3}(?:(?:[*+-]|\d{1,9}[.)])[ \t]+|>[ \t]?))+")
DOUBLE_BLANK = re.compile(_py(r"\n[ \t]*\n[ \t]*$"))
ANY_LINE = re.compile(r"\n[^\n\r\u2028\u2029]*\n")
ANY_PUNCTUATION = re.compile(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~])")


def _nonspace_index(value: str) -> int:
    for index, ch in enumerate(value):
        if ch != " ":
            return index
    return -1


def _rtrim(value: str, ch: str) -> str:
    end = len(value)
    while end > 0 and value[end - 1] == ch:
        end -= 1
    return value[:end]


def _expand_tabs(value: str, column: int = 0) -> str:
    out = []
    for ch in value:
        if ch == "\t":
            width = 4 - column % 4
            out.append(" " * width)
            column += width
        else:
            out.append(ch)
            column += 1
    return "".join(out)


def _trim_blank_tail(value: str) -> str:
    lines = value.split("\n")
    last = len(lines) - 1
    while last >= 0 and re.fullmatch(r"[ \t]*", lines[last]):
        last -= 1
    return value if len(lines) - last <= 2 else "\n".join(lines[:last + 1])


def _indent_compensation(raw: str, text: str) -> str:
    match = re.match(r"^(\s+)(?:```)", raw)
    if match is None:
        return text
    indent = len(match.group(1))
    lines = []
    for line in text.split("\n"):
        space = re.match(r"^\s+", line)
        lines.append(line if space is None else line[min(len(space.group(0)), indent):])
    return "\n".join(lines)


def _bounded(template: str, indent: int, flags: int = 0) -> re.Pattern:
    return re.compile(_py(template.replace("{n}", str(max(0, min(3, indent - 1))))), flags)


def _split_cells(row: str, count: int | None = None) -> list[str]:
    def pipe(match: re.Match) -> str:
        escaped = False
        position = match.start() - 1
        while position >= 0 and row[position] == "\\":
            escaped = not escaped
            position -= 1
        return "|" if escaped else " |"

    cells = re.sub(r"\|", pipe, row).split(" |")
    if cells and not cells[0].strip(JS_WHITESPACE):
        cells.pop(0)
    if cells and not cells[-1].strip(JS_WHITESPACE):
        cells.pop()
    if count is not None:
        if len(cells) > count:
            del cells[count:]
        while len(cells) < count:
            cells.append("")
    return [cell.strip(JS_WHITESPACE).replace("\\|", "|") for cell in cells]


class BlockLexer:
    """Marked's block lexer (GFM, non-pedantic)."""

    def __init__(self):
        self.top = True
        self.last_token: dict | None = None
        self.links: dict[str, dict] = {}

    def lex(self, src: str) -> list[dict]:
        return self.block_tokens(re.sub(r"\r\n|\r", "\n", src), [])

    def block_tokens(self, src: str, tokens: list[dict], clipped: bool = False) -> list[dict]:
        saved_last = self.last_token
        while src:
            self.last_token = tokens[-1] if tokens else None
            last = self.last_token
            token = self.space(src)
            if token is not None:
                src = src[len(token["raw"]):]
                if len(token["raw"]) == 1 and last is not None:
                    last["raw"] += "\n"
                else:
                    tokens.append(token)
                continue
            token = self.code(src)
            if token is not None:
                src = src[len(token["raw"]):]
                if last is not None and last["type"] in ("paragraph", "text"):
                    last["raw"] += ("" if last["raw"].endswith("\n") else "\n") + token["raw"]
                    last["text"] += "\n" + token["text"]
                else:
                    tokens.append(token)
                continue
            for method in (self.fences, self.heading, self.hr, self.blockquote, self.list, self.html):
                token = method(src)
                if token is not None:
                    break
            if token is not None:
                src = src[len(token["raw"]):]
                tokens.append(token)
                continue
            token = self.definition(src)
            if token is not None:
                src = src[len(token["raw"]):]
                if last is not None and last["type"] in ("paragraph", "text"):
                    last["raw"] += ("" if last["raw"].endswith("\n") else "\n") + token["raw"]
                    last["text"] += "\n" + token["raw"]
                else:
                    self.links.setdefault(token["tag"], {"href": token["href"], "title": token["title"]})
                    tokens.append(token)
                continue
            token = self.table(src) or self.lheading(src)
            if token is not None:
                src = src[len(token["raw"]):]
                tokens.append(token)
                continue
            if self.top:
                token = self.paragraph(src)
                if token is not None:
                    if clipped and last is not None and last["type"] == "paragraph":
                        last["raw"] += ("" if last["raw"].endswith("\n") else "\n") + token["raw"]
                        last["text"] += "\n" + token["text"]
                    else:
                        tokens.append(token)
                    clipped = False
                    src = src[len(token["raw"]):]
                    continue
            token = self.text(src)
            if token is not None:
                src = src[len(token["raw"]):]
                if last is not None and last["type"] == "text":
                    last["raw"] += ("" if last["raw"].endswith("\n") else "\n") + token["raw"]
                    last["text"] += "\n" + token["text"]
                else:
                    tokens.append(token)
                continue
            raise ValueError(f"Infinite loop on byte: {ord(src[0])}")
        self.top = True
        self.last_token = saved_last
        return tokens

    # -- tokenizers ----------------------------------------------------------
    def space(self, src):
        match = NEWLINE.match(src)
        if match and match.group(0):
            return {"type": "space", "raw": match.group(0)}
        return None

    def code(self, src):
        match = CODE.match(src)
        if not match:
            return None
        raw = _trim_blank_tail(match.group(0))
        return {"type": "code", "raw": raw, "lang": None, "text": CODE_REMOVE_INDENT.sub("", raw)}

    def fences(self, src):
        match = FENCES.match(src)
        if not match:
            return None
        raw = match.group(0)
        lang = match.group(2)
        if lang:
            lang = ANY_PUNCTUATION.sub(r"\1", lang.strip(JS_WHITESPACE))
        return {"type": "code", "raw": raw, "lang": lang,
                "text": _indent_compensation(raw, match.group(3) or "")}

    def heading(self, src):
        match = HEADING.match(src)
        if not match:
            return None
        text = match.group(2).strip(JS_WHITESPACE)
        if text.endswith("#"):
            trimmed = _rtrim(text, "#")
            if not trimmed or trimmed[-1] in " \t":
                text = trimmed.strip(JS_WHITESPACE)
        return {"type": "heading", "raw": _rtrim(match.group(0), "\n"), "depth": len(match.group(1)),
                "text": text}

    def hr(self, src):
        match = HR.match(src)
        return {"type": "hr", "raw": _rtrim(match.group(0), "\n")} if match else None

    def blockquote(self, src):
        match = BLOCKQUOTE.match(src)
        if not match:
            return None
        lines = _rtrim(match.group(0), "\n").split("\n")
        raw = ""
        text = ""
        tokens: list[dict] = []
        while lines:
            in_quote = False
            current: list[str] = []
            position = 0
            while position < len(lines):
                if BLOCKQUOTE_START.match(lines[position]):
                    current.append(lines[position])
                    in_quote = True
                elif not in_quote:
                    current.append(lines[position])
                else:
                    break
                position += 1
            lines = lines[position:]
            current_raw = "\n".join(current)
            current_text = BQ_MARKER_WS.sub(lambda m: m.group(1) + _expand_tabs(m.group(2), len(m.group(1))),
                                            current_raw)
            current_text = BQ_SETEXT.sub(lambda m: "\n    " + m.group(1), current_text)
            current_text = BQ_SETEXT2.sub("", current_text)
            raw = f"{raw}\n{current_raw}" if raw else current_raw
            text = f"{text}\n{current_text}" if text else current_text
            saved_top = self.top
            self.top = True
            self.block_tokens(current_text, tokens, True)
            self.top = saved_top
            if not lines:
                break
            last = tokens[-1] if tokens else None
            if last is not None and last["type"] in ("code", "heading", "hr", "html", "table"):
                break
            if last is not None and last["type"] == "blockquote":
                joined = last["raw"] + "\n" + BQ_SETEXT2.sub("", "\n".join(lines))
                replacement = self.blockquote(joined)
                tokens[-1] = replacement
                rest = re.sub(r"^\n", "", joined[len(replacement["raw"]):])
                remaining = len(rest.split("\n")) if rest else 0
                absorbed = lines[:-remaining] if remaining else lines
                if absorbed:
                    raw = f"{raw}\n" + "\n".join(absorbed)
                text = text[:len(text) - len(last["text"])] + replacement["text"]
                lines = lines[len(absorbed):]
                if lines and BLOCKQUOTE_START.match(lines[0]):
                    continue
                break
            if last is not None and last["type"] == "list":
                joined = last["raw"] + "\n" + "\n".join(lines)
                replacement = self.list(joined)
                tokens[-1] = replacement
                count = (len(joined[len(last["raw"]) + 1:len(replacement["raw"])].split("\n"))
                         if len(replacement["raw"]) > len(last["raw"]) else 0)
                if count > 0:
                    absorbed = "\n".join(lines[:count])
                    raw = f"{raw}\n{absorbed}"
                    text = f"{text}\n{absorbed}"
                lines = lines[count:]
                continue
        return {"type": "blockquote", "raw": raw, "tokens": tokens, "text": text}

    def list(self, src):
        match = LIST.match(src)
        if not match:
            return None
        if (self.last_token is not None and self.last_token["type"] == "text"
                and not LIST_INTERRUPTS.match(src)):
            return None
        original = src
        bullet = match.group(1).strip(JS_WHITESPACE)
        ordered = len(bullet) > 1
        token = {"type": "list", "raw": "", "ordered": ordered, "loose": False, "items": []}
        bullet_src = (r"\d{1,9}" + "\\" + bullet[-1]) if ordered else ("\\" + bullet)
        item_re = re.compile(_py(r"^( {0,3}" + bullet_src + r")((?:[\t ][^\n]*)?(?:\n|$))"))
        ends_with_blank = False
        while src:
            ended_early = False
            contents = ""
            match = item_re.match(src)
            if not match or HR.match(src):
                break
            item_start = len(original) - len(src)
            src = src[len(match.group(0)):]
            line = match.group(2).split("\n", 1)[0]
            bullet_width = len(match.group(1))
            first = re.sub(r"^[ \t]+", lambda m: _expand_tabs(m.group(0), bullet_width), line)
            next_line = src.split("\n", 1)[0]
            blank = not first.strip(JS_WHITESPACE)
            if blank:
                indent = bullet_width + 1
            else:
                indent = _nonspace_index(first)
                indent = 1 if indent > 4 else indent
                contents = first[indent:]
                indent += bullet_width
            if blank and re.fullmatch(r"[ \t]*", next_line):
                src = src[len(next_line) + 1:]
                ended_early = True
            if not ended_early:
                next_bullet = _bounded(r"^ {0,{n}}(?:[*+-]|\d{1,9}[.)])((?:[ \t][^\n]*)?(?:\n|$))", indent)
                hr = _bounded(r"^ {0,{n}}((?:-[ \t]*){3,}|(?:_[ \t]*){3,}|(?:\*[ \t]*){3,})(?:\n+|$)", indent)
                fences = _bounded(r"^ {0,{n}}(?:```|~~~)", indent)
                heading = _bounded(r"^ {0,{n}}#{1,6}(?:\s|$)", indent)
                html = _bounded(r"^ {0,{n}}(?:</?(?:" + TAGS + r")(?: +|$|/?>)|<(?:(?:script|pre|style|textarea)"
                                r"(?:[\s>]|$)|!--)|<\?|<![A-Za-z]|<!\[CDATA\[)", indent, re.IGNORECASE)
                quote = _bounded(r"^ {0,{n}}>", indent)
                lazy = False
                while src:
                    raw_line = src.split("\n", 1)[0]
                    current = raw_line
                    expanded = re.sub(r"^[ \t]+", lambda m: _expand_tabs(m.group(0)), current)
                    if (fences.match(current) or heading.match(current) or html.match(current)
                            or quote.match(current) or next_bullet.match(current) or hr.match(current)):
                        break
                    if _nonspace_index(expanded) >= indent or not current.strip(JS_WHITESPACE):
                        contents += "\n" + expanded[indent:]
                        lazy = False
                    else:
                        if (blank or _nonspace_index(first.replace("\t", "    ")) >= 4 or fences.match(first)
                                or heading.match(first) or html.match(first) or hr.match(first)):
                            break
                        if not lazy:
                            stripped = NESTED_CONTAINER_MARKERS.sub("", first)
                            if FENCES.match(stripped) or HEADING.match(stripped) or HR.match(stripped):
                                break
                        if re.fullmatch(r" {0,3}(?:=+|-+) *", current) and LHEADING.match(f"{first}\n{current}"):
                            contents += "\n" + "    " + re.sub(r"^ {0,3}", "", expanded, count=1)
                        else:
                            contents += "\n" + current
                        lazy = True
                    blank = not current.strip(JS_WHITESPACE)
                    src = src[len(raw_line) + 1:]
                    first = expanded[indent:]
            item_raw = original[item_start:len(original) - len(src)]
            if not token["loose"]:
                if ends_with_blank:
                    token["loose"] = True
                elif DOUBLE_BLANK.search(item_raw):
                    ends_with_blank = True
            token["items"].append({"type": "list_item", "raw": item_raw, "text": contents, "tokens": []})
        if not token["items"]:
            return None
        last = token["items"][-1]
        last["raw"] = last["raw"].rstrip(JS_WHITESPACE)
        last["text"] = last["text"].rstrip(JS_WHITESPACE)
        token["raw"] = original[:len(original) - len(src)].rstrip(JS_WHITESPACE)
        for item in token["items"]:
            self.top = False
            item["tokens"] = self.block_tokens(item["text"], [])
            if not token["loose"]:
                token["loose"] = any(t["type"] == "space" and ANY_LINE.search(t["raw"]) for t in item["tokens"])
        if token["loose"]:
            for item in token["items"]:
                for child in item["tokens"]:
                    if child["type"] == "text":
                        child["type"] = "paragraph"
        return token

    def html(self, src):
        match = HTML.match(src)
        if not match:
            return None
        raw = _trim_blank_tail(match.group(0))
        return {"type": "html", "raw": raw, "text": raw}

    def definition(self, src):
        match = DEF.match(src)
        if not match:
            return None
        href = match.group(2)
        if not href.startswith("<") and _find_closing(href, "()") != -1:
            return None
        tag = re.sub(r"\s+", " ", match.group(1).strip(JS_WHITESPACE)).lower()
        return {"type": "def", "raw": _rtrim(match.group(0), "\n"), "tag": tag, "href": href,
                "title": match.group(3)}

    def table(self, src):
        match = TABLE.match(src)
        if not match or not re.search(r"[:|]", match.group(2)):
            return None
        header = _split_cells(match.group(1))
        aligns = re.sub(r"^\||\| *$", "", match.group(2)).split("|")
        if len(header) != len(aligns):
            return None
        return {"type": "table", "raw": _rtrim(match.group(0), "\n")}

    def lheading(self, src):
        match = LHEADING.match(src)
        if not match:
            return None
        return {"type": "heading", "raw": _rtrim(match.group(0), "\n"),
                "depth": 1 if match.group(2)[0] == "=" else 2, "text": match.group(1).strip(JS_WHITESPACE)}

    def paragraph(self, src):
        match = PARAGRAPH.match(src)
        if not match:
            return None
        text = match.group(1)
        return {"type": "paragraph", "raw": match.group(0), "text": text[:-1] if text.endswith("\n") else text}

    def text(self, src):
        match = TEXT.match(src)
        return {"type": "text", "raw": match.group(0), "text": match.group(0)} if match else None


def _find_closing(value: str, pair: str) -> int:
    if pair[0] not in value and pair[1] not in value:
        return -1
    depth = 0
    index = 0
    while index < len(value):
        ch = value[index]
        if ch == "\\":
            index += 2
            continue
        if ch == pair[0]:
            depth += 1
        elif ch == pair[1]:
            depth -= 1
            if depth < 0:
                return index
        index += 1
    return -2 if depth > 0 else -1


def walk_tokens(tokens: list[dict]):
    """Every block token in document order, descending into quotes and list items."""
    for token in tokens:
        yield token
        if token["type"] == "list":
            for item in token["items"]:
                yield from walk_tokens(item["tokens"])
        elif token["type"] == "blockquote":
            yield from walk_tokens(token["tokens"])


# ---------------------------------------------------------------------------
# HTML rendering (for Markdown labels)
# ---------------------------------------------------------------------------

_ENTITY_OR_AMP = re.compile(r"&(?!#\d{1,7};|#[Xx][a-fA-F0-9]{1,6};|\w+;)")


def escape(text: str, encode: bool = False) -> str:
    if encode:
        text = text.replace("&", "&amp;")
    else:
        text = _ENTITY_OR_AMP.sub("&amp;", text)
    return (text.replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))


def to_html(src: str) -> str:
    """``marked.parse(src)`` for the text-relevant subset of Markdown."""
    lexer = BlockLexer()
    return _render_blocks(lexer.lex(src))


def _render_blocks(tokens: list[dict]) -> str:
    out = []
    for token in tokens:
        kind = token["type"]
        if kind == "paragraph":
            out.append(f"<p>{render_inline(token['text'])}</p>\n")
        elif kind == "text":
            out.append(render_inline(token["text"]))
        elif kind == "heading":
            out.append(f"<h{token['depth']}>{render_inline(token['text'])}</h{token['depth']}>\n")
        elif kind == "hr":
            out.append("<hr>\n")
        elif kind == "code":
            code = re.sub(r"\n$", "", token["text"]) + "\n"
            lang = re.match(r"\S*", token.get("lang") or "").group(0)
            attr = f' class="language-{escape(lang)}"' if lang else ""
            out.append(f"<pre><code{attr}>{escape(code, True)}</code></pre>\n")
        elif kind == "blockquote":
            out.append(f"<blockquote>\n{_render_blocks(token['tokens'])}</blockquote>\n")
        elif kind == "list":
            tag = "ol" if token["ordered"] else "ul"
            out.append(f"<{tag}>\n</{tag}>\n")
        elif kind == "table":
            out.append("<table>\n</table>\n")
        elif kind == "html":
            out.append(token["text"])
    return "".join(out)


_AUTOLINK = re.compile(r"<([a-zA-Z][a-zA-Z0-9+.-]{1,31}:[^\s\x00-\x1f<>]*|[a-zA-Z0-9.!#$%&'*+/=?_`{|}~-]+"
                       r"(@)[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}"
                       r"[a-zA-Z0-9])?)+(?![-_]))>")
_RAW_HTML = re.compile(r"<!--(?:-?>|[\s\S]*?(?:-->|\Z))|<\?[\s\S]*?\?>|<![a-zA-Z]+\s[\s\S]*?>"
                       r"|<!\[CDATA\[[\s\S]*?\]\]>|</[a-zA-Z][\w:-]*\s*>"
                       r"|<[a-zA-Z][\w-]*(?:\s+[a-zA-Z:_][\w.:-]*(?:\s*=\s*\"[^\"]*\"|\s*=\s*'[^']*'"
                       r"|\s*=\s*[^\s\"'=<>`]+)?)*?\s*/?>")
_ASCII_PUNCT = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"


def _is_punct(ch: str) -> bool:
    return bool(ch) and (ch in _ASCII_PUNCT or unicodedata.category(ch)[0] in "PS")


def _is_space(ch: str) -> bool:
    return ch == "" or ch in " \t\n\r\f" or unicodedata.category(ch) == "Zs"


def _link_end(src: str, start: int) -> tuple[int, str, int] | None:
    """For ``[`` at ``start``: (index of ``]``, inner text, index after ``)``) of an inline link."""
    depth = 0
    index = start
    while index < len(src):
        ch = src[index]
        if ch == "\\":
            index += 2
            continue
        if ch == "`":
            run = re.match(r"`+", src[index:]).group(0)
            close = src.find(run, index + len(run))
            if close >= 0:
                index = close + len(run)
                continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                break
        index += 1
    else:
        return None
    close = index
    if not src.startswith("(", close + 1):
        return None
    depth = 0
    position = close + 1
    while position < len(src):
        ch = src[position]
        if ch == "\\":
            position += 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return close, src[start + 1:close], position + 1
        elif ch == "\n" and src[position + 1:position + 2] == "\n":
            return None
        position += 1
    return None


def _destination(inner: str) -> tuple[str, str | None]:
    inner = inner.strip()
    match = re.match(r"^(<[^>\n]*>|\S*)(?:\s+(\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*$", inner, re.S)
    if match is None:
        return inner, None
    href = match.group(1)
    if href.startswith("<") and href.endswith(">"):
        href = href[1:-1]
    title = match.group(2)[1:-1] if match.group(2) else None
    return href, title


def _plain(html: str) -> str:
    return re.sub(r"<[^>]*>", "", html)


def render_inline(src: str) -> str:
    nodes: list[list] = []  # [kind, html] or ['delim', char, count, can_open, can_close, open_tags, close_tags, orig]
    buffer: list[str] = []

    def flush():
        if buffer:
            nodes.append(["html", escape("".join(buffer))])
            buffer.clear()

    index = 0
    length = len(src)
    while index < length:
        ch = src[index]
        if ch == "\\" and index + 1 < length:
            following = src[index + 1]
            if following == "\n":
                flush()
                nodes.append(["html", "<br>"])
                index += 2
                while index < length and src[index] in " \t":
                    index += 1
                continue
            if following in _ASCII_PUNCT:
                buffer.append(following)
                index += 2
                continue
        if ch == "`":
            run = re.match(r"`+", src[index:]).group(0)
            found = -1
            for candidate in re.finditer(r"`+", src[index + len(run):]):
                if len(candidate.group(0)) == len(run):
                    found = index + len(run) + candidate.start()
                    break
            if found >= 0:
                code = src[index + len(run):found].replace("\n", " ")
                if code.strip(" ") and code.startswith(" ") and code.endswith(" ") and len(code) > 1:
                    code = code[1:-1]
                flush()
                nodes.append(["html", f"<code>{escape(code, True)}</code>"])
                index = found + len(run)
                continue
            buffer.append(run)
            index += len(run)
            continue
        if ch == "<":
            match = _AUTOLINK.match(src, index)
            if match:
                flush()
                text = match.group(1)
                nodes.append(["html", f'<a href="{escape(text)}">{escape(text)}</a>'])
                index = match.end()
                continue
            match = _RAW_HTML.match(src, index)
            if match:
                flush()
                nodes.append(["html", match.group(0)])
                index = match.end()
                continue
        if ch == "[" or (ch == "!" and src.startswith("[", index + 1)):
            image = ch == "!"
            found = _link_end(src, index + 1 if image else index)
            if found is not None:
                close, text, end = found
                href, title = _destination(src[close + 2:end - 1])
                flush()
                title_attr = f' title="{escape(title)}"' if title else ""
                if image:
                    alt = escape(_plain(render_inline(text)), True)
                    nodes.append(["html", f'<img src="{escape(href)}" alt="{alt}"{title_attr}>'])
                else:
                    nodes.append(["html", f'<a href="{escape(href)}"{title_attr}>{render_inline(text)}</a>'])
                index = end
                continue
        if ch == "\n":
            text = "".join(buffer)
            buffer.clear()
            if text.endswith("  "):
                buffer.append(text.rstrip(" "))
                flush()
                nodes.append(["html", "<br>"])
            else:
                buffer.append(text.rstrip(" ") + "\n")
            index += 1
            continue
        if ch in "*_~":
            end = index
            while end < length and src[end] == ch:
                end += 1
            before = src[index - 1] if index > 0 else ""
            after = src[end] if end < length else ""
            left = not _is_space(after) and (not _is_punct(after) or _is_space(before) or _is_punct(before))
            right = not _is_space(before) and (not _is_punct(before) or _is_space(after) or _is_punct(after))
            if ch == "_":
                can_open = left and (not right or _is_punct(before))
                can_close = right and (not left or _is_punct(after))
            else:
                can_open, can_close = left, right
            flush()
            count = end - index
            nodes.append(["delim", ch, count, can_open, can_close, [], [], count])
            index = end
            continue
        buffer.append(ch)
        index += 1
    flush()
    _process_emphasis(nodes)
    out = []
    for node in nodes:
        if node[0] == "html":
            out.append(node[1])
        else:
            out.append("".join(node[6]) + node[1] * node[2] + "".join(node[5]))
    return "".join(out)


def _process_emphasis(nodes: list[list]) -> None:
    delimiters = [i for i, node in enumerate(nodes) if node[0] == "delim"]
    for position, closer_index in enumerate(delimiters):
        closer = nodes[closer_index]
        while closer[4] and closer[2] > 0:
            opener_index = None
            for candidate in reversed(delimiters[:position]):
                opener = nodes[candidate]
                if opener[1] != closer[1] or not opener[3] or opener[2] <= 0:
                    continue
                if closer[1] == "~":
                    if opener[2] != closer[2] or closer[2] > 2:
                        continue
                elif ((opener[4] or closer[3]) and (opener[7] + closer[7]) % 3 == 0
                      and not (opener[7] % 3 == 0 and closer[7] % 3 == 0)):
                    continue
                opener_index = candidate
                break
            if opener_index is None:
                break
            opener = nodes[opener_index]
            if closer[1] == "~":
                use, tag = closer[2], "del"
            else:
                use = 2 if opener[2] >= 2 and closer[2] >= 2 else 1
                tag = "strong" if use == 2 else "em"
            opener[2] -= use
            closer[2] -= use
            opener[5].insert(0, f"<{tag}>")
            closer[6].append(f"</{tag}>")
            for between in delimiters:
                if opener_index < between < closer_index:
                    nodes[between][3] = nodes[between][4] = False
