"""A hand-written, standard-library reader for Mermaid flowchart source.

This module reimplements the parts of the MIT-licensed ``@mmd-toolkit/parser``
0.3.1 that the mermaid-paths adapter relied on: the source rules shared by every
Mermaid diagram (frontmatter, ``%%`` comment lines, ``%%{...}%%`` directives,
line terminators, entities, HTML attribute quotes and the ``style``/``classDef``
semicolon rule), diagram-type detection, and the flowchart lexer and grammar.

It builds only the small tree the adapter reads: statements with their kinds,
vertices (identifier, shape name and label, class marker, data blocks), links
(arrow markers, line kind, label, edge id), accessibility values, and source
positions. Anything the grammar cannot read raises :class:`ParseError`, whose
``loc`` gives a 1-based line and a 0-based UTF-16 column, as the original did.

Non-flowchart diagrams are recognised by their header keyword only: they are
reported as such (and rejected by the adapter) without parsing their bodies.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
import re
from typing import Any

from ._letters import is_unicode_letter

# ---------------------------------------------------------------------------
# Character classes
# ---------------------------------------------------------------------------


def is_whitespace(cp: int) -> bool:
    return (9 <= cp <= 13 or cp == 32 or cp == 160 or cp == 5760 or 8192 <= cp <= 8202
            or cp in (8232, 8233, 8239, 8287, 12288, 65279))


def is_ascii_letter(cp: int) -> bool:
    return 65 <= cp <= 90 or 97 <= cp <= 122


def is_ascii_digit(cp: int) -> bool:
    return 48 <= cp <= 57


def is_word_character(cp: int) -> bool:
    return is_ascii_letter(cp) or is_ascii_digit(cp) or cp == 95


def is_line_whitespace(cp: int) -> bool:
    return is_whitespace(cp) and cp not in (10, 13)


def _upper_canonical(cp: int) -> int:
    if cp > 0xFFFF or 0xD800 <= cp <= 0xDFFF:
        return cp
    upper = chr(cp).upper()
    if len(upper) != 1:
        return cp
    upper_cp = ord(upper)
    if cp > 127 and upper_cp <= 127:
        return cp
    return upper_cp


def matches_ignoring_case(left: int, right: int) -> bool:
    return left == right or _upper_canonical(left) == _upper_canonical(right)


class ParseError(Exception):
    """The source is not valid in the supported Mermaid syntax."""

    def __init__(self, message: str, loc: tuple[int, int] | None):
        super().__init__(message)
        self.message = message
        # (line, column): 1-based line, 0-based UTF-16 column.
        self.loc = loc


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


def line_terminator_end(text: str, offset: int) -> int | None:
    if offset >= len(text):
        return None
    ch = text[offset]
    if ch == "\n":
        return offset + 1
    if ch == "\r":
        return offset + 2 if text.startswith("\n", offset + 1) else offset + 1
    return None


class LineIndex:
    def __init__(self, text: str):
        self.text = text
        starts = [0]
        offset = 0
        length = len(text)
        while offset < length:
            ch = text[offset]
            if ch == "\n":
                offset += 1
                starts.append(offset)
            elif ch == "\r":
                offset += 2 if text.startswith("\n", offset + 1) else 1
                starts.append(offset)
            else:
                offset += 1
        self.starts = starts

    def position(self, offset: int) -> tuple[int, int]:
        index = bisect_right(self.starts, offset) - 1
        start = self.starts[index]
        segment = self.text[start:offset]
        column = len(segment) + sum(1 for ch in segment if ord(ch) > 0xFFFF)
        return index + 1, column


# ---------------------------------------------------------------------------
# Frontmatter (common rules)
# ---------------------------------------------------------------------------


class _RawText:
    """The source as frontmatter matching reads it: a CR is read as a line feed."""

    def __init__(self, text: str):
        self.text = text

    def code_point_at(self, offset: int) -> int | None:
        if offset >= len(self.text):
            return None
        cp = ord(self.text[offset])
        return 10 if cp == 13 else cp

    def after(self, offset: int) -> int:
        end = line_terminator_end(self.text, offset)
        return end if end is not None else offset + 1


def _read_line(text, offset):
    current = offset
    while True:
        cp = text.code_point_at(current)
        if cp is None or cp == 10:
            return {"start": offset, "content_end": current,
                    "next": current if cp is None else text.after(current),
                    "has_terminator": cp is not None}
        current = text.after(current)


def _after_while(text, start, accepts):
    at = start
    cp = text.code_point_at(at)
    while cp is not None and accepts(cp):
        at = text.after(at)
        cp = text.code_point_at(at)
    return at


def _is_whitespace_only(text, line):
    return _after_while(text, line["start"], is_line_whitespace) >= line["content_end"]


def _delimiter_end(text, line, indent):
    if not line["has_terminator"]:
        return None
    at = line["start"]
    for expected in [*indent, 45, 45, 45]:
        if text.code_point_at(at) != expected:
            return None
        at = text.after(at)
    return at if _after_while(text, at, is_line_whitespace) == line["content_end"] else None


def match_frontmatter_in(text, start):
    indent = []
    indent_end = start
    cp = text.code_point_at(indent_end)
    while cp is not None and is_line_whitespace(cp):
        indent.append(cp)
        indent_end = text.after(indent_end)
        cp = text.code_point_at(indent_end)
    opening = _read_line(text, start)
    if _delimiter_end(text, opening, indent) is None:
        return None
    last_blank = None
    line = _read_line(text, opening["next"])
    while _is_whitespace_only(text, line):
        if not line["has_terminator"]:
            return None
        last_blank = line
        line = _read_line(text, line["next"])
    first_content = line
    previous = first_content
    while previous["has_terminator"]:
        line = _read_line(text, previous["next"])
        if _delimiter_end(text, line, indent) is not None:
            return _finish_frontmatter(text, start, line)
        previous = line
    if _delimiter_end(text, first_content, indent) is None or last_blank is None:
        return None
    return _finish_frontmatter(text, start, first_content)


def _finish_frontmatter(text, start, closing_line):
    end = closing_line["next"]
    while text.code_point_at(end) is not None:
        line = _read_line(text, end)
        if not _is_whitespace_only(text, line) or not line["has_terminator"]:
            break
        end = line["next"]
    return {"start": start, "end": end}


# ---------------------------------------------------------------------------
# Directives, comment lines and the style/classDef semicolon rule
# ---------------------------------------------------------------------------


def _skip_cps(text: str, offset: int, predicate) -> int:
    length = len(text)
    while offset < length and predicate(ord(text[offset])):
        offset += 1
    return offset


def read_directive(text: str, start: int) -> tuple[int, int] | None:
    """The range of the ``%%{...}%%`` directive starting at ``start``, if one does."""
    if not text.startswith("%%{", start):
        return None
    after_opening = start + 3
    name_start = _skip_cps(text, after_opening, is_whitespace)
    name_end = _skip_cps(text, name_start, is_word_character)
    if name_end == name_start:
        return None
    offset = name_end
    after_name_ws = _skip_cps(text, offset, is_whitespace)
    if after_name_ws < len(text) and text[after_name_ws] == ":":
        offset = after_name_ws + 1
    offset = _skip_cps(text, offset, is_whitespace)
    if offset < len(text) and is_word_character(ord(text[offset])):
        offset = _skip_cps(text, offset, is_word_character)
    else:
        while offset < len(text):
            if text[offset] in "\u2028\u2029" or text.startswith("}%%", offset):
                break
            offset += 1
    value_end = _skip_cps(text, offset, is_whitespace)
    end = value_end + 3 if text.startswith("}%%", value_end) else value_end
    return start, end


def find_directives(text: str, start: int) -> list[tuple[int, int]]:
    found = []
    offset = start
    while True:
        index = text.find("%%{", offset)
        if index < 0:
            return found
        directive = read_directive(text, index)
        if directive is None:
            offset = index + 1
        else:
            found.append(directive)
            offset = directive[1]


@dataclass
class _Segment:
    start: int
    end: int
    directives: dict[int, int] = field(default_factory=dict)
    directive_ends: dict[int, int] = field(default_factory=dict)
    ignored: set[int] = field(default_factory=set)
    region: bool = False


def _segment_character_at(text, segment, offset):
    current = offset
    while True:
        directive_end = segment.directives.get(current)
        if directive_end is not None:
            current = directive_end
        elif current in segment.ignored:
            current += 1
        else:
            break
    if current >= segment.end or current >= len(text):
        return None
    return ord(text[current]), current, current + 1


def _match_keyword(text, segment, offset, keyword):
    current = offset
    for expected in keyword:
        found = _segment_character_at(text, segment, current)
        if found is None or found[0] != ord(expected):
            return None
        current = found[2]
    return current


def _last_semicolon(text, segment, offset):
    found = None
    while True:
        character = _segment_character_at(text, segment, offset)
        if character is None:
            return found
        if character[0] == 59:
            found = character[1]
        offset = character[2]


def _matches_line_rule(text, segment, keyword):
    offset = segment.start
    while True:
        after_keyword = _match_keyword(text, segment, offset, keyword)
        if after_keyword is not None:
            break
        character = _segment_character_at(text, segment, offset)
        if character is None:
            return False
        offset = character[2]
    offset = after_keyword
    while True:
        character = _segment_character_at(text, segment, offset)
        if character is None:
            return False
        if character[0] == 58:
            stop = _segment_character_at(text, segment, character[2])
            while stop is not None and stop[0] != 35 and not is_whitespace(stop[0]):
                stop = _segment_character_at(text, segment, stop[2])
            if stop is None:
                return False
            if stop[0] == 35:
                return _last_semicolon(text, segment, stop[2]) is not None
            offset = stop[1]
            continue
        offset = character[2]


def _decide_ignored_semicolons(text, segment):
    plain = not segment.directives
    for keyword in ("style", "classDef"):
        if plain and (keyword not in text[segment.start:segment.end] or ";" not in text[segment.start:segment.end]):
            continue
        if not _matches_line_rule(text, segment, keyword):
            continue
        semicolon = _last_semicolon(text, segment, segment.start)
        if semicolon is not None:
            segment.ignored.add(semicolon)


def _make_segment(text, start, end, directives, region=False):
    segment = _Segment(start, end, region=region)
    for d_start, d_end in directives:
        segment.directives[d_start] = d_end
        segment.directive_ends[d_end] = d_start
    if not region:
        _decide_ignored_semicolons(text, segment)
    return segment


def _pass_directives(text, offset, directives):
    while True:
        directive = read_directive(text, offset)
        if directive is None:
            return offset
        directives.append(directive)
        offset = directive[1]


def _look_ahead(text: str, start: int) -> list[_Segment]:
    """Comment lines: one comment region, or the line segments up to the next separator."""
    directives: list[tuple[int, int]] = []
    separators: list[tuple[int, int]] = []
    offset = start
    length = len(text)
    while offset < length:
        directive = read_directive(text, offset)
        if directive is not None:
            directives.append(directive)
            offset = directive[1]
            continue
        terminator_end = line_terminator_end(text, offset)
        if terminator_end is not None:
            separators.append((offset, terminator_end))
            offset = terminator_end
            continue
        ch = text[offset]
        if ch in "\u2028\u2029":
            separators.append((offset, offset + 1))
            offset += 1
            continue
        if ch == "%":
            second = _pass_directives(text, offset + 1, directives)
            if not text.startswith("%", second):
                return _segments_up_to(text, start, separators, directives, second)
            after_marker = second + 1
            following = _pass_directives(text, after_marker, directives)
            if following < length and text[following] not in "{\r\n":
                return [_comment_region(text, start, following, directives)]
            return _segments_up_to(text, start, separators, directives, following)
        if is_whitespace(ord(ch)):
            offset += 1
            continue
        return _segments_up_to(text, start, separators, directives, offset)
    return _segments_up_to(text, start, separators, directives, length)


_REGION_STOPS = re.compile("%%\\{|[\\r\\n]")


def _comment_region(text, start, resume_at, directives):
    offset = resume_at
    length = len(text)
    while offset < length:
        found = _REGION_STOPS.search(text, offset)
        if found is None:
            offset = length
            break
        offset = found.start()
        if text[offset] != "%":
            break
        directive = read_directive(text, offset)
        if directive is not None:
            directives.append(directive)
            offset = directive[1]
        else:
            offset += 1
    end = line_terminator_end(text, offset)
    return _make_segment(text, start, end if end is not None else offset, directives, region=True)


_SEGMENT_STOPS = re.compile("%%\\{|[\\r\\n\\u2028\\u2029]")


def _segments_up_to(text, start, separators, directives, resume_at):
    offset = resume_at
    length = len(text)
    while offset < length:
        found = _SEGMENT_STOPS.search(text, offset)
        if found is None:
            offset = length
            break
        offset = found.start()
        if text[offset] != "%":
            break
        directive = read_directive(text, offset)
        if directive is not None:
            directives.append(directive)
            offset = directive[1]
        else:
            offset += 1
    ranges = []
    next_start = start
    for sep_start, sep_end in separators:
        ranges.append((next_start, sep_start))
        next_start = sep_end
    ranges.append((next_start, offset))
    entries = []
    taken = 0
    for seg_start, seg_end in ranges:
        after = taken
        while after < len(directives) and directives[after][1] <= seg_end:
            after += 1
        entries.append(_make_segment(text, seg_start, seg_end, directives[taken:after]))
        taken = after
    return entries


def _next_comment_start(text, entries):
    last = entries[-1]
    if last.region:
        return last.end
    if last.end >= len(text):
        return len(text)
    end = line_terminator_end(text, last.end)
    return end if end is not None else last.end + 1


def find_attribute_quotes(text: str, start: int) -> set[int]:
    """HTML attribute quotes: the ``"`` of every ``="..."`` inside a ``<tag ...>``."""
    quotes: set[int] = set()
    offset = start
    length = len(text)
    while offset < length:
        tag_start = text.find("<", offset)
        if tag_start < 0:
            break
        name_start = tag_start + 1
        name_end = _skip_cps(text, name_start, is_word_character)
        if name_end == name_start:
            offset = name_start
            continue
        tag_end = text.find(">", name_end)
        if tag_end < 0:
            break
        attribute_offset = name_end
        while attribute_offset < tag_end:
            opening = text.find('="', attribute_offset, tag_end)
            if opening < 0:
                break
            opening_quote = opening + 1
            closing_quote = text.find('"', opening_quote + 1, tag_end)
            if closing_quote < 0:
                break
            quotes.add(opening_quote)
            quotes.add(closing_quote)
            attribute_offset = closing_quote + 1
        offset = tag_end + 1
    return quotes


# ---------------------------------------------------------------------------
# The shared source reader
# ---------------------------------------------------------------------------

_NO_RULE = ("none", 0)
_READ_STOPS = re.compile("[\r\n#;]")
PLACEHOLDER_OPENING = "\ufb02\u00b0"
PLACEHOLDER_NUMERIC_OPENING = "\ufb02\u00b0\u00b0"
PLACEHOLDER_CLOSING = "\u00b6\u00df"


class Source:
    """The source text, after its frontmatter, with the rules every diagram shares."""

    def __init__(self, text: str):
        self.text = text
        self.lines = LineIndex(text)
        fm = match_frontmatter_in(_RawText(text), 0)
        self.has_frontmatter = fm is not None
        self.start = fm["end"] if fm is not None else 0
        self.attribute_quotes = find_attribute_quotes(text, self.start)
        self.segments: list[_Segment] = []
        offset = self.start
        while offset < len(text):
            entries = _look_ahead(text, offset)
            self.segments.extend(entries)
            offset = _next_comment_start(text, entries)
        self.segment_starts = [segment.start for segment in self.segments]
        self.has_directives = any(segment.directives for segment in self.segments)
        # The rule at each offset where one applies: a comment region's start, an
        # ignored `;`, or a directive's start. Offsets inside a region are never asked.
        self.special: dict[int, tuple[str, int]] = {}
        self.directive_ends: dict[int, int] = {}
        for segment in self.segments:
            if segment.region:
                self.special[segment.start] = ("region", segment.end)
                continue
            for semicolon in segment.ignored:
                self.special[semicolon] = ("ignored", semicolon + 1)
            for d_start, d_end in segment.directives.items():
                self.special[d_start] = ("directive", d_end)
                self.directive_ends[d_end] = d_start
        self.special_offsets = sorted(self.special)

    # -- rules at an offset ------------------------------------------------
    def _segment_at(self, offset: int) -> _Segment | None:
        if offset < self.start or offset >= len(self.text):
            return None
        index = bisect_right(self.segment_starts, offset) - 1
        if index < 0:
            return None
        segment = self.segments[index]
        return segment if offset < segment.end else None

    def rule_at(self, offset: int) -> tuple[str, int]:
        """('region', end) | ('ignored', offset+1) | ('directive', end) | ('none', 0)."""
        return self.special.get(offset, _NO_RULE)

    def next_special(self, offset: int) -> int:
        """The first offset at or after ``offset`` where a rule applies (or the text length)."""
        index = bisect_left(self.special_offsets, offset)
        return self.special_offsets[index] if index < len(self.special_offsets) else len(self.text)

    def location(self, offset: int) -> tuple[int, int]:
        return self.lines.position(offset)

    def skip_directives_and_comments(self, offset: int) -> int:
        while offset < len(self.text):
            rule, end = self.rule_at(offset)
            if rule in ("region", "directive"):
                offset = end
            else:
                return offset
        return len(self.text)

    def skip_trivia(self, offset: int) -> int:
        while offset < len(self.text):
            rule, end = self.rule_at(offset)
            if rule in ("region", "directive"):
                offset = end
            elif rule == "none" and is_whitespace(ord(self.text[offset])):
                offset += 1
            else:
                return offset
        return len(self.text)

    def is_ignored_semicolon(self, offset: int) -> bool:
        return self.rule_at(offset)[0] == "ignored"

    def _character_from(self, offset: int) -> int | None:
        start = self.skip_directives_and_comments(offset)
        return start if start < len(self.text) else None

    def _character_before(self, offset: int) -> int | None:
        end = offset
        while True:
            if end <= self.start:
                return None
            directive_start = self.directive_ends.get(end)
            if directive_start is None:
                break
            end = directive_start
        return end - 1

    def entity_delimiter_at(self, offset: int) -> tuple[str, int | None] | None:
        ch = self.text[offset]
        if ch == "#":
            has_word = False
            numeric = True
            following = self._character_from(offset + 1)
            while following is not None and is_word_character(ord(self.text[following])):
                has_word = True
                if not is_ascii_digit(ord(self.text[following])):
                    numeric = False
                following = self._character_from(following + 1)
            if (not has_word or following is None or self.text[following] != ";"
                    or self.is_ignored_semicolon(following)):
                return None
            return (PLACEHOLDER_NUMERIC_OPENING if numeric else PLACEHOLDER_OPENING), following + 1
        if ch == ";":
            if self.is_ignored_semicolon(offset):
                return None
            has_word = False
            preceding = self._character_before(offset)
            while preceding is not None and is_word_character(ord(self.text[preceding])):
                has_word = True
                preceding = self._character_before(preceding)
            if not has_word or preceding is None or self.text[preceding] != "#":
                return None
            return PLACEHOLDER_CLOSING, None
        return None

    def text_of(self, start: int, end: int, left_out: list[tuple[int, int]] = ()) -> str:
        """The written text of a range, without comment regions, directives and ignored ``;``."""
        left_out = sorted(left_out)
        parts = []
        offset = start
        index = 0
        text = self.text
        while offset < end:
            while index < len(left_out) and left_out[index][1] <= offset:
                index += 1
            if index < len(left_out) and left_out[index][0] <= offset:
                offset = left_out[index][1]
                continue
            rule, rule_end = self.special.get(offset, _NO_RULE)
            if rule != "none":
                offset = rule_end
                continue
            stop = min(end, self.next_special(offset + 1))
            if index < len(left_out):
                stop = min(stop, max(left_out[index][0], offset + 1))
            parts.append(text[offset:stop])
            offset = stop
        return "".join(parts)


@dataclass
class Chars:
    """The diagram text as the flowchart lexer reads it, one entry per character."""
    cps: list[int]
    starts: list[int]
    ends: list[int]
    eot: list[bool]
    placeholder_end: dict[int, int]


def read_diagram_text(source: Source, start: int) -> Chars:
    text = source.text
    cps: list[int] = []
    starts: list[int] = []
    ends: list[int] = []
    eot: list[bool] = []
    placeholder_end: dict[int, int] = {}
    offset = start
    length = len(text)
    quotes = source.attribute_quotes
    quote_offsets = sorted(offset_ for offset_ in quotes if offset_ >= start)
    quote_index = 0
    while offset < length:
        rule, rule_end = source.special.get(offset, _NO_RULE)
        if rule != "none":
            offset = rule_end
            continue
        while quote_index < len(quote_offsets) and quote_offsets[quote_index] < offset:
            quote_index += 1
        stop = source.next_special(offset)
        if quote_index < len(quote_offsets):
            stop = min(stop, quote_offsets[quote_index])
        found = _READ_STOPS.search(text, offset, stop)
        if found is not None:
            stop = found.start()
        if stop > offset:
            run = text[offset:stop]
            cps.extend(map(ord, run))
            starts.extend(range(offset, stop))
            ends.extend(range(offset + 1, stop + 1))
            eot.extend([False] * (stop - offset))
            offset = stop
            continue
        terminator_end = line_terminator_end(text, offset)
        if terminator_end is not None:
            cps.append(10)
            starts.append(offset)
            ends.append(terminator_end)
            eot.append(False)
            offset = terminator_end
            continue
        if offset in quotes:
            converted = "'"
            p_end = None
        else:
            ch = text[offset]
            delimiter = source.entity_delimiter_at(offset) if ch in "#;" else None
            if delimiter is not None:
                converted, p_end = delimiter
            else:
                converted, p_end = ch, None
        if p_end is not None:
            placeholder_end[len(cps)] = p_end
        for converted_ch in converted:
            cps.append(ord(converted_ch))
            starts.append(offset)
            ends.append(offset + 1)
            eot.append(False)
        offset += 1
    cps.append(10)
    starts.append(length)
    ends.append(length)
    eot.append(True)
    # Whitespace after `}`: a run that holds a line feed is read, through its last
    # line feed, as one line feed.
    if 125 not in cps:
        return Chars(cps, starts, ends, eot, placeholder_end)
    out_cps: list[int] = []
    out_starts: list[int] = []
    out_ends: list[int] = []
    out_eot: list[bool] = []
    out_placeholder: dict[int, int] = {}
    index = 0
    total = len(cps)
    while index < total:
        if index in placeholder_end:
            out_placeholder[len(out_cps)] = placeholder_end[index]
        out_cps.append(cps[index])
        out_starts.append(starts[index])
        out_ends.append(ends[index])
        out_eot.append(eot[index])
        if cps[index] == 125:
            run = index + 1
            last_lf = None
            while run < total and is_whitespace(cps[run]):
                if cps[run] == 10:
                    last_lf = run
                run += 1
            if last_lf is not None:
                out_cps.append(10)
                out_starts.append(starts[index + 1])
                out_ends.append(ends[last_lf])
                out_eot.append(eot[last_lf])
                index = last_lf + 1
                continue
        index += 1
    return Chars(out_cps, out_starts, out_ends, out_eot, out_placeholder)


# ---------------------------------------------------------------------------
# Diagram detection
# ---------------------------------------------------------------------------

_OTHER_DETECTORS: tuple[tuple[str, str, str], ...] = (
    # (keyword, comparison, what may follow)
    ("mindmap", "exact", "any"), ("architecture", "exact", "any"),
    ("agentflow-beta", "exact", "nonword"), ("C4Context", "exact", "any"),
    ("C4Container", "exact", "any"), ("C4Component", "exact", "any"),
    ("C4Dynamic", "exact", "any"), ("C4Deployment", "exact", "any"), ("kanban", "exact", "any"),
    ("classDiagram", "exact", "any"), ("erDiagram", "exact", "any"), ("gantt", "exact", "any"),
    ("info", "exact", "any"), ("pie", "exact", "any"), ("requirement", "exact", "any"),
    ("sequenceDiagram", "exact", "any"),
)
_LATER_DETECTORS: tuple[tuple[str, str, str], ...] = (
    ("timeline", "exact", "any"), ("gitGraph", "exact", "any"), ("stateDiagram", "exact", "any"),
    ("journey", "exact", "any"), ("quadrantChart", "exact", "any"), ("sankey", "exact", "any"),
    ("packet", "exact", "any"), ("xychart", "exact", "any"), ("block", "exact", "any"),
    ("eventmodeling", "exact", "any"), ("treeView-beta", "exact", "any"),
    ("radar-beta", "exact", "any"), ("ishikawa", "case", "nonword"), ("treemap", "exact", "any"),
    ("railroad-beta", "case", "any"), ("railroad-ebnf-beta", "case", "any"),
    ("railroad-abnf-beta", "case", "any"), ("railroad-peg-beta", "case", "any"),
    ("venn-beta", "exact", "any"), ("wardley-beta", "case", "any"),
    ("cynefin-beta", "exact", "wscolon"), ("usecase-beta", "exact", "ws"),
)


def _character_at(source: Source, offset: int) -> tuple[int | None, int, int]:
    start = source.skip_directives_and_comments(offset)
    text = source.text
    if start >= len(text):
        return None, len(text), len(text)
    end = line_terminator_end(text, start)
    if end is not None:
        return 10, start, end
    return ord(text[start]), start, start + 1


def _keyword_end(source, position, keyword, compare):
    offset = position
    for expected in keyword:
        cp, _, end = _character_at(source, offset)
        if cp is None or not compare(cp, ord(expected)):
            return None
        offset = end
    return offset


def _detect(source, position, keyword, comparison, follow):
    compare = (lambda a, b: a == b) if comparison == "exact" else matches_ignoring_case
    end = _keyword_end(source, position, keyword, compare)
    if end is None:
        return False
    after = _character_at(source, end)[0]
    if follow == "any":
        return True
    if follow == "nonword":
        return after is None or not is_word_character(after)
    if follow == "wscolon":
        return after is None or after == 58 or is_whitespace(after)
    return after is None or is_whitespace(after)


class _DetectionText:
    def __init__(self, source: Source):
        self.source = source

    def code_point_at(self, offset):
        return _character_at(self.source, offset)[0]

    def after(self, offset):
        return _character_at(self.source, offset)[2]


def _remove_percent_runs(text: str) -> str:
    result = []
    kept_start = 0
    offset = 0
    while offset < len(text):
        marker = text.find("%%", offset)
        if marker < 0:
            break
        line_break_end = None
        for index in range(marker + 2, len(text)):
            end = line_terminator_end(text, index)
            if end is not None:
                line_break_end = end
                break
        if line_break_end is None:
            break
        whitespace_start = marker
        while whitespace_start > kept_start and is_whitespace(ord(text[whitespace_start - 1])):
            whitespace_start -= 1
        result.append(text[kept_start:whitespace_start] + "\n")
        kept_start = line_break_end
        offset = line_break_end
    return "".join(result) + text[kept_start:]


def _is_error_pseudo_diagram(source: Source, detection_point: int) -> bool:
    if not re.search("error", source.text[detection_point:], re.IGNORECASE):
        return False
    parts = []
    offset = detection_point
    while True:
        cp, start, end = _character_at(source, offset)
        if cp is None:
            break
        parts.append("\n" if line_terminator_end(source.text, start) is not None
                     else source.text[start:end])
        offset = end
    diagram_text = "".join(parts)
    fm = match_frontmatter_in(_RawText(diagram_text), 0)
    if fm is not None:
        diagram_text = diagram_text[fm["end"]:]
    kept = []
    offset = 0
    for d_start, d_end in find_directives(diagram_text, 0):
        kept.append(diagram_text[offset:d_start])
        offset = d_end
    kept.append(diagram_text[offset:])
    condition = _remove_percent_runs("".join(kept))
    stripped = condition
    begin = _skip_cps(stripped, 0, is_whitespace)
    word = stripped[begin:begin + 5]
    if len(word) != 5 or not all(matches_ignoring_case(ord(a), ord(b)) for a, b in zip(word, "error")):
        return False
    return _skip_cps(stripped, begin + 5, is_whitespace) == len(stripped)


def detect(source: Source) -> tuple[str, int]:
    """The detected diagram family ('flowchart' or 'other') and where its text starts."""
    detection_point = source.skip_trivia(source.start)
    if _is_error_pseudo_diagram(source, detection_point):
        return "other", detection_point
    block = match_frontmatter_in(_DetectionText(source), detection_point)
    position = detection_point if block is None else source.skip_trivia(block["end"])
    while True:
        cp, start, end = _character_at(source, position)
        if cp != 37:
            break
        cp2, _, end2 = _character_at(source, end)
        if cp2 != 37:
            break
        cp3, start3, _ = _character_at(source, end2)
        if not (cp3 is None or cp3 == 123 or line_terminator_end(source.text, start3) is not None):
            break
        offset = end2
        while True:
            cp4, start4, end4 = _character_at(source, offset)
            if cp4 is None or line_terminator_end(source.text, start4) is not None:
                next_offset = end4
                break
            offset = end4
        position = source.skip_trivia(next_offset)
    frontmatter_error = "Diagrams beginning with --- are not valid: the block is not frontmatter"
    if _keyword_end(source, position, "---", lambda a, b: a == b) is not None:
        raise ParseError(frontmatter_error, source.location(detection_point if block is not None else position))
    detected = None
    if _detect(source, position, "flowchart-elk", "exact", "any"):
        detected = "flowchart"
    else:
        for keyword, comparison, follow in _OTHER_DETECTORS:
            if _detect(source, position, keyword, comparison, follow):
                detected = "other"
                break
    if detected is None and (_detect(source, position, "swimlane-beta", "exact", "nonword")
                             or _detect(source, position, "graph", "exact", "any")
                             or _detect(source, position, "flowchart", "exact", "any")):
        detected = "flowchart"
    if detected is None:
        for keyword, comparison, follow in _LATER_DETECTORS:
            if _detect(source, position, keyword, comparison, follow):
                detected = "other"
                break
    if detected is not None:
        if detected == "flowchart" and block is not None:
            raise ParseError(frontmatter_error, source.location(detection_point))
        return detected, position
    raise ParseError("No diagram type detected", source.location(detection_point))


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


class Unit:
    __slots__ = ("name", "index", "start", "end", "text", "sig", "nonws", "content", "arrow")

    def __init__(self, name, index, start, end, text, sig, nonws, content=None, arrow=None):
        self.name = name
        self.index = index
        self.start = start
        self.end = end
        self.text = text
        self.sig = sig
        self.nonws = nonws
        self.content = content
        self.arrow = arrow

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"Unit({self.name!r}, {self.text!r})"


ARROW_LINES = {"dash": "solid", "thick": "thick", "dotted": "dotted"}
STATEMENT_KEYWORDS = ("style", "default", "linkStyle", "interpolate", "classDef", "class")
LINK_TARGETS = ("_self", "_blank", "_parent", "_top")
DIRECTION_VALUES = ("TB", "BT", "RL", "LR", "TD")
HEADER_DIRECTION_WORDS = ("LR", "RL", "TB", "BT", "TD", "BR", "v")
HEADER_KEYWORDS = ("flowchart-elk", "swimlane-beta", "graph", "flowchart")
STATEMENT_PUNCTUATION = ("#", ":::", ":", "&", ";", ",", "*")
SHAPE_OPENERS = (("(-", "ellipse"), ("([", "label"), ("[[", "label"), (">", "label"),
                 ("[(", "label"), ("(((", "label"), ("[|", None), ("[/", "slanted"),
                 ("[\\", "slanted"), ("^", None))
ID_RUN_PUNCTUATION = frozenset((33, 34, 35, 36, 37, 38, 39, 42, 43, 46, 96, 63, 92, 95, 47))
EDGE_ID_RUN_ENDS = frozenset((34, 91, 93, 40, 41, 123, 125, 124))
LABEL_CLOSERS = ("])", "]]", ")]", ")))", "|", ")", "]", "}")
LINK_TEXT_FORBIDDEN = {"dash": 45, "thick": 61, "dotted": 46}
END_OF_TEXT_MESSAGES = {
    "callback-name": "The callback after call has no ( before the end of the text",
    "callback-arguments": "The arguments of the callback have no ) before the end of the text",
}


_WS_CHARS = "\t\n\x0b\x0c\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_WS_RUN = re.compile("[" + _WS_CHARS + "]*")
_NOT_WS_RUN = re.compile("[^" + _WS_CHARS + "]*")
_EDGE_ID_RUN = re.compile("[^" + _WS_CHARS + "\"\\[\\](){}|]*")
_ID_RUN = re.compile(r"(?:[A-Za-z0-9!\"#$%&'*+.`?\\_/]|-(?=[^>\-.]))+")
_LABEL_TEXT = re.compile(r"[^\[\](){}|\"]+")
_SLANTED_TEXT = re.compile(r"[^\\\[\](){}/]+")
_DATA_TEXT = re.compile(r"[^}^\"]+")
_MARKDOWN_RUN = re.compile(r"[^`\"]+")
_DIGITS = re.compile(r"[0-9]+")
_LINE_END = re.compile("[\n\u2028\u2029]")


def _is_id_run_character(cp: int) -> bool:
    return is_ascii_letter(cp) or is_ascii_digit(cp) or cp in ID_RUN_PUNCTUATION


class FlowchartLexer:
    """Divides the diagram text into units with the flowchart's stack of contexts."""

    def __init__(self, chars: Chars, source: Source):
        self.c = chars
        self.cps = chars.cps
        self.n = len(chars.cps)
        self.s = "".join(map(chr, chars.cps))
        self.source = source
        self.units: list[Unit] = []
        self.pos = 0
        self.contexts = ["statement"]
        self.entries: list[tuple[int, int] | None] = []
        self.header_read = False
        self.no_edge_id_before = 0
        self.no_direction_before = 0

    # -- helpers -------------------------------------------------------------
    def is_cp(self, p: int, cp: int) -> bool:
        return p < self.n and self.cps[p] == cp

    def is_ws_at(self, p: int) -> bool:
        return p < self.n and is_whitespace(self.cps[p])

    def is_word_at(self, p: int) -> bool:
        return p < self.n and is_word_character(self.cps[p])

    def skip_ws(self, p: int) -> int:
        return _WS_RUN.match(self.s, p).end() if p < self.n else p

    def find(self, p: int, ch: str) -> int:
        """The first index of ``ch`` at or after ``p``, or the end of the text."""
        index = self.s.find(ch, p)
        return self.n if index < 0 else index

    def run(self, pattern: re.Pattern, p: int) -> int:
        match = pattern.match(self.s, p)
        return p if match is None else match.end()

    def skip_while(self, p: int, predicate) -> int:
        cps, n = self.cps, self.n
        while p < n and predicate(cps[p]):
            p += 1
        return p

    def word(self, p: int, word: str) -> int | None:
        cps, n = self.cps, self.n
        for ch in word:
            if p >= n or cps[p] != ord(ch):
                return None
            p += 1
        return p

    def whole_word(self, p: int, word: str) -> int | None:
        end = self.word(p, word)
        if end is None or self.is_word_at(end):
            return None
        return end

    def error_at(self, p: int, message: str = "Unexpected character") -> ParseError:
        start = self.c.starts[p] if p < self.n else len(self.source.text)
        return ParseError(message, self.source.location(start))

    def add_unit(self, name, frm, to, text=None, content=None, ends="trim", arrow=None) -> Unit:
        c = self.c
        cps = self.cps
        start = c.starts[frm] if frm < self.n else len(self.source.text)
        end = c.ends[to - 1] if to > frm else start
        keep = ends == "keep"
        first = frm
        while first < to and ((not keep and is_whitespace(cps[first])) or c.eot[first]):
            first += 1
        sig = None
        if first < to:
            last = to - 1
            while (not keep and is_whitespace(cps[last])) or c.eot[last]:
                last -= 1
            sig = (c.starts[first], c.ends[last])
        first = frm
        while first < to and is_whitespace(cps[first]):
            first += 1
        nonws = None
        if first < to:
            last = to - 1
            while is_whitespace(cps[last]):
                last -= 1
            nonws = (c.starts[first], c.ends[last])
        unit = Unit(name, len(self.units), start, end, self.s[frm:to] if text is None else text, sig, nonws,
                    content, arrow)
        self.units.append(unit)
        return unit

    def read(self, name, at, to, ends="trim") -> Unit:
        unit = self.add_unit(name, at, to, ends=ends)
        self.pos = to
        return unit

    def enter(self, context, entry):
        self.contexts.append(context)
        self.entries.append(entry)

    def leave(self):
        if len(self.contexts) > 1:
            self.contexts.pop()
            self.entries.pop()

    # -- main loop ---------------------------------------------------------
    def lex(self) -> list[Unit]:
        while self.pos < self.n:
            self.read_in_context(self.pos, self.contexts[-1])
        message = END_OF_TEXT_MESSAGES.get(self.contexts[-1])
        if message is not None and self.entries and self.entries[-1] is not None:
            raise ParseError(message, self.source.location(self.entries[-1][0]))
        return self.units

    def read_in_context(self, at, context):
        if context == "statement":
            self.read_statement(at)
        elif context == "header":
            self.read_header(at)
        elif context == "title":
            self.read_rest_of_line(at, "title-text")
        elif context == "description":
            self.read_rest_of_line(at, "description-text")
        elif context == "description-block":
            if self.cps[at] == 125:
                self.read("}", at, at + 1)
                self.leave()
            else:
                self.read("description-block-text", at, self.find(at, "}"))
        elif context == "data":
            self.read_data(at)
        elif context == "data-string":
            if self.cps[at] == 34:
                self.read("data-quote", at, at + 1)
                self.leave()
            else:
                self.read("data-text", at, self.find(at, '"'), ends="keep")
        elif context == "click":
            self.read_click(at)
        elif context == "callback-name":
            self.read_callback_name(at)
        elif context == "callback-arguments":
            if self.cps[at] == 41:
                self.read(")", at, at + 1)
                self.leave()
            else:
                self.read("callback-arguments", at, self.find(at, ")"), ends="keep")
        elif context == "label":
            self.read_label(at)
        elif context == "slanted":
            self.read_slanted(at)
        elif context == "ellipse":
            self.read_ellipse(at)
        else:
            self.read_link_text(at, context.split("-", 1)[0])

    # -- statement context -------------------------------------------------
    def read_statement(self, at):
        if self.rules_before_arrow(at):
            return
        if is_whitespace(self.cps[at]):
            self.read_whitespace_run(at)
            return
        if not (self.read_arrow(at) or self.read_shape_opener(at) or self.read_letter_v(at)
                or self.read_id_run(at) or self.read_minus(at) or self.read_letter(at)
                or self.read_label_opener(at)):
            raise self.error_at(at)

    def rules_before_arrow(self, at) -> bool:
        return (self.read_accessibility_marker(at) or self.read_data_start(at)
                or self.read_call_keyword(at) or self.read_string(at)
                or self.read_statement_keyword(at) or self.read_href(at) or self.read_click_keyword(at)
                or self.read_header_keyword(at) or self.read_subgraph(at) or self.read_end(at)
                or self.read_link_target(at) or self.read_direction_line(at)
                or self.read_edge_id(at) or self.read_number(at) or self.read_punctuation(at))

    def _sig(self, unit):
        return unit.sig

    def read_accessibility_marker(self, at) -> bool:
        title = self.word(at, "accTitle")
        if title is not None:
            colon = self.skip_ws(title)
            if self.is_cp(colon, 58):
                keyword = self.add_unit("accTitle", at, title)
                self.add_unit(":", colon, colon + 1)
                self.pos = self.skip_ws(colon + 1)
                self.enter("title", keyword.sig)
                return True
        description = self.word(at, "accDescr")
        if description is None:
            return False
        mark = self.skip_ws(description)
        if self.is_cp(mark, 58) or self.is_cp(mark, 123):
            keyword = self.add_unit("accDescr", at, description)
            colon = self.is_cp(mark, 58)
            self.add_unit(":" if colon else "{", mark, mark + 1)
            self.pos = self.skip_ws(mark + 1)
            self.enter("description" if colon else "description-block", keyword.sig)
            return True
        return False

    def read_data_start(self, at) -> bool:
        end = self.word(at, "@{")
        if end is None:
            return False
        unit = self.read("data-start", at, end)
        self.enter("data", unit.sig)
        return True

    def read_call_keyword(self, at) -> bool:
        end = self.word(at, "call")
        if end is None or not self.is_ws_at(end):
            return False
        call = self.add_unit("call", at, end)
        self.pos = self.skip_ws(end)
        self.enter("callback-name", call.sig)
        return True

    def read_statement_keyword(self, at) -> bool:
        for keyword in STATEMENT_KEYWORDS:
            end = self.whole_word(at, keyword)
            if end is not None:
                self.read(keyword, at, end)
                return True
        return False

    def read_href(self, at) -> bool:
        end = self.word(at, "href")
        if end is None or not self.is_ws_at(end):
            return False
        self.add_unit("href", at, end)
        self.pos = end + 1
        return True

    def read_click_keyword(self, at) -> bool:
        end = self.word(at, "click")
        if end is None or not self.is_ws_at(end):
            return False
        click = self.add_unit("click", at, end)
        self.pos = self.skip_ws(end)
        self.enter("click", click.sig)
        if self.pos >= self.n:
            raise ParseError("click has no click id before the end of the text",
                             self.source.location(click.sig[0]))
        return True

    def read_header_keyword(self, at) -> bool:
        for keyword in HEADER_KEYWORDS:
            end = self.whole_word(at, keyword)
            if end is not None:
                unit = self.read("header-keyword", at, end)
                if not self.header_read:
                    self.enter("header", unit.sig)
                self.header_read = True
                return True
        return False

    def read_subgraph(self, at) -> bool:
        end = self.whole_word(at, "subgraph")
        if end is None:
            return False
        self.read("subgraph", at, end)
        return True

    def read_end(self, at) -> bool:
        end = self.whole_word(at, "end")
        if end is None:
            return False
        self.read("end", at, self.skip_ws(end))
        return True

    def read_link_target(self, at) -> bool:
        for target in LINK_TARGETS:
            end = self.whole_word(at, target)
            if end is not None:
                self.read("link-target", at, end)
                return True
        return False

    def read_direction_line(self, at) -> bool:
        if at < self.no_direction_before:
            return False
        n = self.n
        line_match = _LINE_END.search(self.s, at)
        run_end = line_match.start() if line_match else n
        last_by_value: dict[str, tuple[int, int, int, int]] = {}
        keyword = self.s.find("direction", at, run_end + 9)
        while 0 <= keyword < run_end:
            keyword_end = keyword + 9
            if self.is_ws_at(keyword_end):
                value = self.skip_ws(keyword_end)
                for candidate in DIRECTION_VALUES:
                    value_end = self.word(value, candidate)
                    if value < n and value_end is not None:
                        last_by_value[candidate] = (keyword, keyword_end, value, value_end)
            keyword = self.s.find("direction", keyword + 1, run_end + 9)
        found = next((last_by_value[v] for v in DIRECTION_VALUES if v in last_by_value), None)
        if found is None:
            self.no_direction_before = run_end
            return False
        keyword, keyword_end, value, value_end = found
        line_end = self.find(value_end, "\n")
        if not self._is_blank(at, keyword):
            self.add_unit("direction-text", at, keyword)
        self.add_unit("direction", keyword, keyword_end)
        self.add_unit("direction-value", value, value_end)
        if not self._is_blank(value_end, line_end):
            self.add_unit("direction-text", value_end, line_end)
        self.pos = line_end
        return True

    def _is_blank(self, frm, to) -> bool:
        p = frm
        while p < to and is_whitespace(self.cps[p]):
            p += 1
        return p == to

    def read_edge_id(self, at) -> bool:
        if at < self.no_edge_id_before:
            return False
        run_end = self.run(_EDGE_ID_RUN, at)
        last_at = None
        search_end = run_end
        while True:
            index = self.s.rfind("@", at + 1, search_end)
            if index < 0:
                break
            if index + 1 < self.n and self.s[index + 1] not in '{"':
                last_at = index
                break
            search_end = index
        if last_at is None:
            self.no_edge_id_before = run_end
            return False
        self.add_unit("edge-id", at, last_at)
        self.read("@", last_at, last_at + 1)
        return True

    def read_number(self, at) -> bool:
        end = self.run(_DIGITS, at)
        if end == at:
            return False
        self.read("number", at, end)
        return True

    def read_punctuation(self, at) -> bool:
        for punctuation in STATEMENT_PUNCTUATION:
            end = self.word(at, punctuation)
            if end is not None:
                self.read(punctuation, at, end)
                return True
        return False

    def read_arrow(self, at) -> bool:
        arrow = self.match_arrow(at)
        if arrow is None:
            return False
        self.read_arrow_from(at, arrow)
        return True

    def read_arrow_from(self, start, arrow):
        end_pos, parts = arrow
        end = self.skip_ws(end_pos)
        name = "arrow" if parts["form"] == "closed" else "opening-arrow"
        unit = self.add_unit(name, start, end, arrow=parts)
        self.pos = end
        if parts["form"] == "opening":
            self.enter(parts["opens"] + "-link-text", unit.sig)

    def read_whitespace_run(self, at):
        arrow = self.match_arrow(self.skip_ws(at))
        if arrow is not None:
            self.read_arrow_from(at, arrow)
            return
        start = at
        cps, n = self.cps, self.n
        while True:
            line_feeds = cps[start] == 10
            end = start
            while end < n and is_whitespace(cps[end]) and (cps[end] == 10) == line_feeds:
                end += 1
            self.read("newline" if line_feeds else "space", start, end)
            if not self.is_ws_at(end) or self.rules_before_arrow(end):
                return
            start = end

    def read_shape_opener(self, at) -> bool:
        for name, enters in SHAPE_OPENERS:
            end = self.word(at, name)
            if end is not None:
                unit = self.read(name, at, end)
                if enters is not None:
                    self.enter(enters, unit.sig)
                return True
        if self.word(at, "\\|") is not None:
            raise self.error_at(at)
        return False

    def read_letter_v(self, at) -> bool:
        if self.cps[at] != 118 or self.is_word_at(at + 1):
            return False
        self.read("v", at, at + 1)
        return True

    def read_id_run(self, at) -> bool:
        p = self.run(_ID_RUN, at)
        if p == at:
            return False
        self.read("id-run", at, p)
        return True

    def read_minus(self, at) -> bool:
        if self.cps[at] != 45:
            return False
        self.read("-", at, at + 1)
        return True

    def read_letter(self, at) -> bool:
        if not is_unicode_letter(self.cps[at]):
            return False
        self.read("letter", at, at + 1)
        return True

    def read_label_opener(self, at) -> bool:
        for opener in ("|", "(", "[", "{"):
            if self.cps[at] == ord(opener):
                unit = self.read(opener, at, at + 1)
                self.enter("label", unit.sig)
                return True
        return False

    # -- strings -------------------------------------------------------------
    def read_string(self, at) -> bool:
        if self.cps[at] != 34:
            return False
        cps, n = self.cps, self.n
        contexts: list[str] = []
        runs: list[tuple[str, int, int]] = []
        position = self._enter_string(at, contexts)
        while contexts:
            if position >= n:
                raise self.error_at(at)
            if contexts[-1] == "string":
                if cps[position] == 34:
                    following = position + 1
                    if self.is_cp(following, 96):
                        position = self._enter_string(position, contexts)
                    else:
                        contexts.pop()
                        position = following
                else:
                    end = self.find(position, '"')
                    runs.append(("string", position, end))
                    position = end
            elif cps[position] == 96:
                following = position + 1
                if not self.is_cp(following, 34):
                    raise self.error_at(position)
                contexts.pop()
                position = following + 1
            elif cps[position] == 34:
                position = self._enter_string(position, contexts)
            else:
                end = self.run(_MARKDOWN_RUN, position)
                runs.append(("markdown-string", position, end))
                position = end
        if len(runs) > 1:
            raise self.error_at(at)
        if not runs:
            self.add_unit("empty-string", at, position, text="")
        else:
            name, run_start, run_end = runs[0]
            text = self.s[run_start:run_end]
            content = (self.c.starts[run_start],
                       self.c.ends[run_end - 1] if run_end > run_start else self.c.starts[run_start])
            self.add_unit(name, at, position, text=text, content=content)
        self.pos = position
        return True

    def _enter_string(self, quote, contexts) -> int:
        following = quote + 1
        if self.is_cp(following, 96):
            contexts.append("markdown-string")
            return following + 1
        contexts.append("string")
        return following

    # -- other contexts ------------------------------------------------------
    def read_header(self, at):
        if self.read_string(at):
            return
        cps = self.cps
        last_lf = None
        direction = at
        while self.is_ws_at(direction):
            if cps[direction] == 10:
                last_lf = direction
            direction += 1
        if last_lf is not None:
            self.read("no-direction", at, last_lf + 1)
            self.leave()
            return
        if direction >= self.n:
            raise self.error_at(at)
        for word in HEADER_DIRECTION_WORDS:
            end = self.whole_word(direction, word)
            if end is not None:
                self.read("header-direction", direction, end)
                self.leave()
                return
        if cps[direction] in (60, 62, 94):
            self.read("header-direction", direction, direction + 1)
            self.leave()
            return
        raise self.error_at(direction)

    def read_rest_of_line(self, at, name):
        if self.cps[at] == 10:
            self.leave()
            return
        self.read(name, at, self.find(at, "\n"))

    def read_data(self, at):
        cp = self.cps[at]
        if cp == 34:
            quote = self.read("data-quote", at, at + 1)
            self.enter("data-string", quote.sig)
            return
        if cp == 125:
            self.read("}", at, at + 1)
            self.leave()
            return
        end = self.run(_DATA_TEXT, at)
        if end == at:
            raise self.error_at(at)
        self.read("data-text", at, end, ends="keep")

    def read_click(self, at):
        if self.read_string(at):
            return
        cps = self.cps
        if is_whitespace(cps[at]):
            self.pos = at + 1
            self.leave()
            return
        end = self.run(_NOT_WS_RUN, at)
        p = at
        while p < end:
            if cps[p] == 44:
                self.add_unit("click-comma", p, p + 1)
                p += 1
            else:
                run_end = p
                while run_end < end and cps[run_end] != 44:
                    run_end += 1
                self.add_unit("click-id", p, run_end)
                p = run_end
        self.pos = end

    def read_callback_name(self, at):
        if self.cps[at] != 40:
            self.read("callback-name", at, self.find(at, "("), ends="keep")
            return
        opening = self.add_unit("(", at, at + 1)
        self.leave()
        closing = self.skip_ws(at + 1)
        if self.is_cp(closing, 41):
            self.read(")", closing, closing + 1)
            return
        self.pos = at + 1
        self.enter("callback-arguments", opening.sig)

    def reject_tildes(self, at):
        p = self.skip_ws(at)
        tildes = 0
        while self.is_cp(p, 126):
            tildes += 1
            p += 1
        if tildes >= 3:
            raise self.error_at(at)

    def read_label(self, at):
        if self.read_string(at):
            return
        self.reject_tildes(at)
        for rejected in ("(-", "([", "((("):
            if self.word(at, rejected) is not None:
                raise self.error_at(at)
        for closer in LABEL_CLOSERS:
            end = self.word(at, closer)
            if end is not None:
                self.read(closer, at, end)
                self.leave()
                return
        for opener in ("(", "{"):
            if self.cps[at] == ord(opener):
                unit = self.read(opener, at, at + 1)
                self.enter("label", unit.sig)
                return
        end = self.run(_LABEL_TEXT, at)
        if end == at:
            raise self.error_at(at)
        self.read("text-run", at, end)

    def read_slanted(self, at):
        if self.read_string(at):
            return
        self.reject_tildes(at)
        cps = self.cps
        following = at + 1
        closes = self.is_cp(following, 93)
        if closes and cps[at] in (92, 40, 63, 61, 93, 41):
            self.read("backslash-end", at, following + 1)
            self.leave()
            return
        if closes and cps[at] == 47:
            self.read("slash-end", at, following + 1)
            self.leave()
            return
        if cps[at] in (47, 92):
            self.read("text-run", at, following)
            return
        end = self.run(_SLANTED_TEXT, at)
        if end == at:
            raise self.error_at(at)
        self.read("text-run", at, end)

    def read_each_whitespace(self, at, name):
        p = at
        while self.is_ws_at(p):
            self.add_unit(name, p, p + 1)
            p += 1
        self.pos = p

    def after_one_character(self, at) -> int:
        placeholder_end = self.c.placeholder_end.get(at)
        end = at + 1
        if placeholder_end is None:
            return end
        while end < self.n and self.c.starts[end] < placeholder_end:
            end += 1
        return end

    def read_ellipse(self, at):
        if self.read_string(at):
            return
        self.reject_tildes(at)
        cps = self.cps
        if is_whitespace(cps[at]):
            self.read_each_whitespace(at, "text-run")
            return
        if cps[at] in (45, 47, 41) and self.is_cp(at + 1, 41):
            self.read("ellipse-end", at, at + 2)
            self.leave()
            return
        if cps[at] in (40, 41, 91, 93, 123, 125):
            raise self.error_at(at)
        self.read("text-run", at, self.after_one_character(at))

    def read_link_text(self, at, kind):
        if self.read_string(at):
            return
        arrow = self.match_closed_arrow(self.skip_ws(at), kind)
        if arrow is not None:
            end = self.skip_ws(arrow[0])
            self.add_unit("arrow", at, end, arrow=arrow[1])
            self.pos = end
            self.leave()
            return
        cps = self.cps
        if is_whitespace(cps[at]):
            self.read_each_whitespace(at, "link-text-char")
            return
        if cps[at] == LINK_TEXT_FORBIDDEN[kind]:
            if not (kind == "dash" and not self.is_cp(at + 1, 45)):
                raise self.error_at(at)
        self.read("link-text-char", at, self.after_one_character(at))

    # -- arrows ----------------------------------------------------------------
    def _start_marker(self, p):
        if self.is_cp(p, 60):
            return "arrow"
        if self.is_cp(p, 120):
            return "cross"
        if self.is_cp(p, 111):
            return "circle"
        return None

    def _end_marker(self, p):
        if self.is_cp(p, 62):
            return "arrow"
        if self.is_cp(p, 120):
            return "cross"
        if self.is_cp(p, 111):
            return "circle"
        return None

    def _count_run(self, p, cp):
        count = 0
        while self.is_cp(p, cp):
            count += 1
            p += 1
        return count, p

    def _closed_line(self, p, kind):
        if kind == "dotted":
            dots, dots_end = self._count_run(p + 1 if self.is_cp(p, 45) else p, 46)
            if dots == 0 or not self.is_cp(dots_end, 45):
                return None
            after_line = dots_end + 1
            marker = self._end_marker(after_line)
            if marker is None:
                return after_line, dots, "none"
            return after_line + 1, dots, marker
        line_cp = 45 if kind == "dash" else 61
        count, end = self._count_run(p, line_cp)
        marker = self._end_marker(end)
        if count >= 2 and end < self.n and marker is not None:
            return end + 1, count, marker
        return (end, count, "none") if count >= 3 else None

    def _opening_line(self, p, kind):
        if kind == "dotted":
            if not self.is_cp(p, 45) or not self.is_cp(p + 1, 46):
                return None
            return p + 2
        line_cp = 45 if kind == "dash" else 61
        if not self.is_cp(p, line_cp) or not self.is_cp(p + 1, line_cp):
            return None
        return p + 2

    def _with_optional_head(self, p, match_line):
        marker = self._start_marker(p)
        if p < self.n and marker is not None:
            with_head = match_line(p + 1)
            if with_head is not None:
                return with_head, marker
        without = match_line(p)
        return None if without is None else (without, "none")

    def match_closed_arrow(self, p, kind):
        match = self._with_optional_head(p, lambda q: self._closed_line(q, kind))
        if match is None:
            return None
        (end, length, end_marker), start_marker = match
        return end, {"form": "closed", "startMarker": start_marker, "line": ARROW_LINES[kind],
                     "length": length, "endMarker": end_marker}

    def match_opening_arrow(self, p, kind):
        match = self._with_optional_head(p, lambda q: self._opening_line(q, kind))
        if match is None:
            return None
        end, start_marker = match
        return end, {"form": "opening", "startMarker": start_marker, "line": ARROW_LINES[kind],
                     "opens": kind}

    def match_arrow(self, p):
        for kind in ("dash", "thick", "dotted"):
            closed = self.match_closed_arrow(p, kind)
            if closed is not None:
                return closed
            opening = self.match_opening_arrow(p, kind)
            if opening is not None:
                return opening
        tildes, end = self._count_run(p, 126)
        if tildes < 3:
            return None
        return end, {"form": "closed", "startMarker": "none", "line": "invisible",
                     "length": tildes, "endMarker": "none"}


# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------

ID_PIECES = frozenset(("number", "id-run", "letter", "v", "-", "default", ",", ":", "&", "#", "*"))
ID_LIST_PART_PIECES = ID_PIECES - {","}
STYLE_PIECES = frozenset(("number", "id-run", ":", "#", "space", "style"))
NAME_UNITS = frozenset(("number", "letter", "id-run", "v", "-", ",", ":", "&", "#", "*"))
NAME_PIECES = frozenset(("number", "id-run", "letter", "space", "-", "&", ":", "*", "#", "v", "^",
                         "style", "linkStyle", "classDef", "class", "header-keyword", "subgraph", "end"))
SHAPE_STARTS = frozenset(("[", "(", "(((", "{", "([", "[[", "[(", ">", "(-", "[/", "[\\", "[|"))
SEPARATORS = ("newline", ";")
EMPTY_FORM_MESSAGES = {
    "empty-string": "An empty string cannot be written here",
    "empty-call": "An empty call cannot be written here",
    "empty-description-block":
        "An empty description block can only be written where a statement starts or ends",
}


@dataclass
class Node:
    """A tree node: ``type`` plus the fields the adapter reads, and its source range."""
    type: str
    range: tuple[int, int]
    fields: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key):
        return self.fields[key]

    def get(self, key, default=None):
        return self.fields.get(key, default)


class FlowchartGrammar:
    def __init__(self, units: list[Unit], source: Source):
        self.units = units
        self.source = source
        self.removed: set[int] = set()
        self.next = 0
        self.edge_count = 0
        self.empty_forms: dict[int, tuple[str, list[Unit]]] = {}
        for index, unit in enumerate(units):
            if unit.name == "empty-string":
                self.empty_forms[index] = ("empty-string", [unit])
                continue
            if index + 2 < len(units):
                second, third = units[index + 1], units[index + 2]
                if unit.name == "call" and second.name == "(" and third.name == ")":
                    form = ("empty-call", [unit, second, third])
                elif unit.name == "accDescr" and second.name == "{" and third.name == "}":
                    form = ("empty-description-block", [unit, second, third])
                else:
                    continue
                for member in form[1]:
                    self.empty_forms[member.index] = form

    # -- errors ------------------------------------------------------------
    def error(self, message, offset):
        return ParseError(message, self.source.location(offset))

    def form_error(self, form):
        kind, members = form
        return self.error(EMPTY_FORM_MESSAGES[kind], members[0].sig[0])

    def reserved_click(self, unit):
        start = unit.sig[0] if unit.sig else unit.start
        return self.error("click is a reserved word: it may only start a click statement", start)

    def unexpected(self, unit: Unit | None) -> ParseError:
        length = len(self.source.text)
        if unit is None:
            return self.error("Unexpected end of text", length)
        following = self.unit_at(self.after_names(self.position_after(unit.index), ("space",)))
        for cause in ([unit] if following is None else [unit, following]):
            if cause.name == "click":
                return self.reserved_click(cause)
            form = self.empty_forms.get(cause.index)
            if form is not None:
                return self.form_error(form)
        start = unit.sig[0] if unit.sig else unit.start
        return self.error("Unexpected end of text" if start == length else "Unexpected token", start)

    # -- navigation --------------------------------------------------------
    def position_after(self, position):
        after = position + 1
        while after < len(self.units) and after in self.removed:
            after += 1
        return after

    def position_before(self, position):
        before = position - 1
        while before >= 0 and before in self.removed:
            before -= 1
        return before

    def unit_at(self, position) -> Unit | None:
        return self.units[position] if 0 <= position < len(self.units) else None

    def name_at(self, position):
        unit = self.unit_at(position)
        return None if unit is None else unit.name

    def peek(self):
        return self.unit_at(self.next)

    def advance(self):
        self.next = self.position_after(self.next)

    def take(self, name) -> Unit:
        unit = self.peek()
        if unit is None or unit.name != name:
            raise self.unexpected(unit)
        self.advance()
        return unit

    def take_if(self, name) -> Unit | None:
        unit = self.peek()
        if unit is None or unit.name != name:
            return None
        self.advance()
        return unit

    def take_while(self, names) -> list[Unit]:
        taken = []
        unit = self.peek()
        while unit is not None and unit.name in names:
            taken.append(unit)
            self.advance()
            unit = self.peek()
        return taken

    def after_names(self, position, names):
        name = self.name_at(position)
        while name is not None and name in names:
            position = self.position_after(position)
            name = self.name_at(position)
        return position

    # -- places and text ---------------------------------------------------
    def place_of(self, units: list[Unit]) -> tuple[int, int]:
        significant = [unit.sig for unit in units if unit.sig is not None]
        if not significant:
            return units[0].start, units[-1].end
        return significant[0][0], significant[-1][1]

    def place_from(self, start) -> tuple[int, int]:
        last = self.position_before(self.next)
        first = start
        while first <= last and self.units[first].sig is None:
            first = self.position_after(first)
        if first > last:
            return self.place_of([self.units[start], self.units[last]])
        final = last
        while self.units[final].sig is None:
            final = self.position_before(final)
        return self.place_of([self.units[first], self.units[final]])

    def text_of(self, range_, left_out=()):
        return self.source.text_of(range_[0], range_[1], list(left_out))

    def identifier(self, units: list[Unit]) -> str:
        return "".join(self.text_of(unit.sig) for unit in units if unit.sig is not None)

    # -- diagram and statements ---------------------------------------------
    def parse_diagram(self) -> Node:
        start = self.next
        self.take("header-keyword")
        direction = None
        header_blocks: list[Node] = []
        if self.take_if("no-direction") is None:
            blocks = self.take_out_statement_end()
            header_direction = self.take("header-direction")
            direction = self.text_of(header_direction.sig)
            if self.take_if(";") is None:
                self.take_while(("space",))
                self.take("newline")
            header_blocks = [self.empty_description_block(units) for units in blocks]
        body = header_blocks + self.parse_lines()
        return Node("FlowchartDiagram", (self.units[start].sig or (0, 0)),
                    {"direction": direction, "body": body})

    def parse_lines(self) -> list[Node]:
        top: list[Node] = []
        open_subgraphs: list[dict] = []
        while True:
            unit = self.peek()
            if unit is None:
                if open_subgraphs:
                    raise self.unexpected(None)
                return top
            statements = open_subgraphs[-1]["body"] if open_subgraphs else top
            if unit.name == "end":
                if not open_subgraphs:
                    raise self.unexpected(unit)
                subgraph = open_subgraphs.pop()
                self.take("end")
                node = Node("FlowchartSubgraphStatement", self.place_from(subgraph["start"]),
                            {"body": subgraph["body"]})
                (open_subgraphs[-1]["body"] if open_subgraphs else top).append(node)
            elif unit.name in ("newline", "space"):
                self.advance()
            elif unit.name == ";":
                start = self.next
                self.take(";")
                statements.append(Node("FlowchartEmptyStatement", self.place_from(start)))
            elif unit.name == "subgraph":
                open_subgraphs.append(self.open_subgraph())
            else:
                statements.extend(self.parse_statement(unit))

    def parse_statement(self, first: Unit) -> list[Node]:
        if first.name in ("direction-text", "direction"):
            return [self.parse_direction()]
        if first.name == "accTitle":
            return [self.parse_accessibility_line("accTitle", "title-text", "FlowchartAccTitleStatement")]
        if first.name == "accDescr":
            if self.name_at(self.position_after(self.next)) != ":":
                return [self.parse_description_block()]
            return [self.parse_accessibility_line("accDescr", "description-text",
                                                  "FlowchartAccDescrStatement")]
        blocks = self.take_out_statement_end()
        statement = self.parse_statement_with_separator(first)
        return [statement, *self.parse_statement_end(blocks)]

    def parse_statement_with_separator(self, first: Unit) -> Node:
        if first.name == "style":
            return self.parse_style()
        if first.name == "classDef":
            return self.parse_class_def()
        if first.name == "class":
            return self.parse_class()
        if first.name == "linkStyle":
            return self.parse_link_style()
        if first.name == "click":
            return self.parse_click()
        return self.parse_vertex_statement()

    def take_out_statement_end(self) -> list[list[Unit]]:
        separator = self.next
        name = self.name_at(separator)
        while name is not None and name not in SEPARATORS:
            separator = self.position_after(separator)
            name = self.name_at(separator)
        blocks: list[list[Unit]] = []
        unit = self._unread_before(separator)
        while unit is not None:
            form = self.empty_forms.get(unit.index)
            if unit.name == "space":
                unit = self._unread_before(unit.index)
            elif form is not None and form[0] == "empty-description-block" and form[1][-1] is unit:
                blocks.append(form[1])
                unit = self._unread_before(form[1][0].index)
            else:
                blocks.reverse()
                for block in blocks:
                    for member in block:
                        self.removed.add(member.index)
                return blocks
        return []

    def _unread_before(self, position):
        before = self.position_before(position)
        return None if before < self.next else self.unit_at(before)

    def parse_statement_end(self, blocks) -> list[Node]:
        separator = self.peek()
        if separator is not None:
            if separator.name not in SEPARATORS:
                raise self.unexpected(separator)
            self.advance()
        return [self.empty_description_block(units) for units in blocks]

    def empty_description_block(self, units) -> Node:
        return Node("FlowchartAccDescrBlockStatement", self.place_of(units),
                    {"value": None, "closed": True})

    # -- vertices and links ------------------------------------------------
    def parse_vertex_statement(self) -> Node:
        start = self.next
        first = self.parse_vertex_group()
        self.take_while(("space",))
        links = []
        source = first
        while self.name_at(self.next) in ("edge-id", "arrow", "opening-arrow"):
            link = self.parse_link()
            self.take_while(("space",))
            self.edge_count += len(source["vertices"]) * len(link["target"]["vertices"])
            links.append(link)
            source = link["target"]
        return Node("FlowchartVertexStatement", self.place_from(start), {"first": first, "links": links})

    def parse_vertex_group(self) -> Node:
        start = self.next
        vertices = [self.parse_vertex()]
        while True:
            ampersand = self.after_names(self.next, ("space",))
            ampersand_unit = self.unit_at(ampersand)
            if ampersand == self.next or ampersand_unit is None or ampersand_unit.name != "&":
                break
            after_ampersand = self.position_after(ampersand)
            vertex = self.after_names(after_ampersand, ("space",))
            unit = self.unit_at(vertex)
            if unit is None:
                break
            form = self.empty_forms.get(unit.index)
            if form is not None:
                raise self.form_error(form)
            if vertex == after_ampersand or self.name_at(vertex) not in ID_PIECES:
                break
            self.next = vertex
            vertices.append(self.parse_vertex())
        return Node("FlowchartVertexGroup", self.place_from(start), {"vertices": vertices})

    def parse_id(self) -> list[Unit]:
        pieces = self.take_while(ID_PIECES)
        if not pieces:
            raise self.unexpected(self.peek())
        return pieces

    def parse_vertex(self) -> Node:
        start = self.next
        id_units = self.parse_id()
        shape = self.parse_shape() if self.name_at(self.next) in SHAPE_STARTS else None
        css_class = None
        if self.take_if(":::") is not None:
            css_class = self.identifier(self.parse_id())
        data = self.parse_data()
        return Node("FlowchartVertex", self.place_from(start), {
            "id": self.identifier(id_units), "shape": shape, "cssClass": css_class, "data": data})

    def parse_data(self) -> list[Node]:
        blocks = []
        while self.name_at(self.next) == "data-start":
            start = self.next
            self.take("data-start")
            self.take_while(("data-text", "data-quote"))
            self.take("}")
            blocks.append(Node("FlowchartDataBlock", self.place_from(start)))
        return blocks

    def parse_shape(self) -> Node:
        start = self.next
        opener = self.peek()
        name = opener.name
        if name == "[":
            return self._delimited(start, ["["], lambda: (["]"], "rectangle"))
        if name == "(":
            if self.name_at(self.position_after(self.next)) == "(":
                return self._delimited(start, ["(", "("], lambda: ([")", ")"], "circle"))
            return self._delimited(start, ["("], lambda: ([")"], "rounded"))
        if name == "(((":
            return self._delimited(start, ["((("], lambda: ([")))"], "double-circle"))
        if name == "{":
            if self.name_at(self.position_after(self.next)) == "{":
                return self._delimited(start, ["{", "{"], lambda: (["}", "}"], "hexagon"))
            return self._delimited(start, ["{"], lambda: (["}"], "diamond"))
        if name == "([":
            return self._delimited(start, ["(["], lambda: (["])"], "stadium"))
        if name == "[[":
            return self._delimited(start, ["[["], lambda: (["]]"], "framed-rectangle"))
        if name == "[(":
            return self._delimited(start, ["[("], lambda: ([")]"], "cylinder"))
        if name == ">":
            return self._delimited(start, [">"], lambda: (["]"], "odd"))
        if name == "(-":
            return self._delimited(start, ["(-"], lambda: (["ellipse-end"], "ellipse"))
        if name == "[/":
            return self._delimited(start, ["[/"], lambda: (
                (["backslash-end"], "trapezoid-bottom") if self.name_at(self.next) == "backslash-end"
                else (["slash-end"], "lean-right")))
        if name == "[\\":
            return self._delimited(start, ["[\\"], lambda: (
                (["slash-end"], "trapezoid-top") if self.name_at(self.next) == "slash-end"
                else (["backslash-end"], "lean-left")))
        if name == "[|":
            self.take("[|")
            self.take("id-run")
            self.take(":")
            self.take("id-run")
            self.take("|")
            label = self.parse_label()
            self.take("]")
            return Node("FlowchartShape", self.place_from(start), {"name": "rectangle", "label": label})
        raise self.unexpected(opener)

    def _delimited(self, start, opening, closing) -> Node:
        for name in opening:
            self.take(name)
        label = self.parse_label()
        closing_names, shape_name = closing()
        for name in closing_names:
            self.take(name)
        return Node("FlowchartShape", self.place_from(start), {"name": shape_name, "label": label})

    def parse_starting_with_string(self, take_pieces) -> Node:
        first = self.peek()
        if first is None:
            raise self.unexpected(None)
        kind = "text"
        delimiters: list[tuple[int, int]] = []
        units: list[Unit] = []
        if first.name in ("string", "markdown-string"):
            kind = "string" if first.name == "string" else "markdown"
            self.advance()
            units.append(first)
            sig = first.sig
            delimiters = [(sig[0], first.content[0]), (first.content[1], sig[1])]
        pieces = take_pieces()
        if not units and not pieces:
            raise self.unexpected(first)
        units.extend(pieces)
        place = self.place_of(units)
        return Node("FlowchartLabel", place, {"kind": kind, "value": self.text_of(place, delimiters)})

    def parse_label(self) -> Node:
        return self.parse_starting_with_string(lambda: self.take_while(("text-run",)))

    def parse_link(self) -> Node:
        start = self.next
        edge_id = None
        unit = self.take_if("edge-id")
        if unit is not None:
            edge_id = self.identifier([unit])
            self.take("@")
        opening = None
        label = None
        opening_unit = self.take_if("opening-arrow")
        if opening_unit is not None:
            opening = opening_unit.arrow
            label = self.parse_starting_with_string(lambda: self.take_while(("link-text-char",)))
        arrow_unit = self.take("arrow")
        arrow = arrow_unit.arrow
        if opening_unit is None and self.take_if("|") is not None:
            label = self.parse_label()
            self.take("|")
            self.take_if("space")
        link_range = self.place_from(start)
        target = self.parse_vertex_group()
        return Node("FlowchartLink", link_range, {
            "id": edge_id, "opening": opening, "arrow": arrow, "label": label, "target": target})

    # -- subgraphs -----------------------------------------------------------
    def open_subgraph(self) -> dict:
        start = self.next
        self.take("subgraph")
        self.take("space")
        blocks = self.take_out_statement_end()
        self.parse_starting_with_string(lambda: self.take_while(NAME_PIECES))
        if self.take_if("[") is not None:
            self.parse_label()
            self.take("]")
        body = self.parse_statement_end(blocks)
        return {"start": start, "body": body}

    # -- styles and classes --------------------------------------------------
    def take_style_pieces(self) -> list[Unit]:
        pieces = self.take_while(STYLE_PIECES)
        if not pieces:
            raise self.unexpected(self.peek())
        return pieces

    def parse_styles(self, in_class_def):
        while True:
            pieces = self.take_style_pieces()
            if in_class_def:
                while pieces[-1].text.endswith("\\") and self.name_at(self.next) == ",":
                    pieces.append(self.take(","))
                    pieces.extend(self.take_style_pieces())
            if self.take_if(",") is None:
                return

    def parse_comma_list(self, part_pieces, comma):
        first = self.peek()
        while True:
            unit = self.peek()
            if unit is None:
                break
            if unit.name == comma or unit.name in part_pieces:
                self.advance()
            else:
                break
        if self.peek() is first:
            raise self.unexpected(first)

    def parse_style(self) -> Node:
        start = self.next
        self.take("style")
        self.take("space")
        self.parse_id()
        self.take("space")
        self.parse_styles(False)
        return Node("FlowchartStyleStatement", self.place_from(start))

    def parse_class_def(self) -> Node:
        start = self.next
        self.take("classDef")
        self.take("space")
        self.parse_comma_list(ID_LIST_PART_PIECES, ",")
        self.take("space")
        self.parse_styles(True)
        return Node("FlowchartClassDefStatement", self.place_from(start))

    def parse_class(self) -> Node:
        start = self.next
        self.take("class")
        self.take("space")
        self.parse_comma_list(ID_LIST_PART_PIECES, ",")
        self.take("space")
        self.parse_id()
        return Node("FlowchartClassStatement", self.place_from(start))

    def parse_name(self):
        units = self.take_while(NAME_UNITS)
        if not units:
            raise self.unexpected(self.peek())

    def parse_link_style(self) -> Node:
        start = self.next
        self.take("linkStyle")
        self.take("space")
        is_default = self.take_if("default") is not None
        if not is_default:
            while True:
                number = self.take("number")
                if (len(number.text) > 1 and number.text.startswith("0")) or int(number.text) >= self.edge_count:
                    raise self.error(f"linkStyle {number.text} does not name a link that exists", number.sig[0])
                if self.take_if(",") is None:
                    break
        self.take("space")
        if self.take_if("interpolate") is not None:
            self.take("space")
            self.parse_name()
            if self.take_if("space") is not None:
                self.parse_styles(False)
        else:
            self.parse_styles(False)
        kind = "FlowchartDefaultLinkStyleStatement" if is_default else "FlowchartLinkStyleStatement"
        return Node(kind, self.place_from(start))

    # -- click ---------------------------------------------------------------
    def parse_click(self) -> Node:
        start = self.next
        click = self.take("click")
        after = self.after_names(self.next, ("empty-string",))
        if self.name_at(after) not in ("click-id", "click-comma"):
            raise self.reserved_click(click)
        if after != self.next:
            raise self.unexpected(self.peek())
        self.parse_comma_list(("click-id",), "click-comma")
        first = self.peek()
        if first is None:
            raise self.unexpected(None)
        if first.name == "call":
            self.parse_callback()
            self.take_space_and_string()
            kind = "FlowchartClickCallbackStatement"
        elif first.name in ("href", "string"):
            self.take_if("href")
            self.take("string")
            self.take_space_and_string()
            if self.name_at(self.next) == "space" and self.name_at(self.position_after(self.next)) == "link-target":
                self.advance()
                self.take("link-target")
            kind = "FlowchartClickUrlStatement"
        else:
            self.parse_name()
            self.take_space_and_string()
            kind = "FlowchartClickCallbackStatement"
        return Node(kind, self.place_from(start))

    def parse_callback(self):
        self.take("call")
        self.take("callback-name")
        self.take("(")
        arguments = self.take_if("callback-arguments")
        self.take(")")
        if arguments is None and self.name_at(self.next) == "call":
            self.take("call")
            self.take("(")
            self.take("callback-arguments")
            self.take(")")

    def take_space_and_string(self):
        if self.name_at(self.next) != "space" or self.name_at(self.position_after(self.next)) != "string":
            return
        self.advance()
        self.take("string")

    # -- direction and accessibility ----------------------------------------
    def parse_direction(self) -> Node:
        start = self.next
        self.take_if("direction-text")
        self.take("direction")
        value = self.take("direction-value")
        self.take_if("direction-text")
        return Node("FlowchartDirectionStatement", self.place_from(start), {"direction": value.text})

    def parse_accessibility_line(self, keyword, text_name, node_type) -> Node:
        start = self.next
        self.take(keyword)
        self.take(":")
        text = self.take_if(text_name)
        return Node(node_type, self.place_from(start), {"value": self._literal(text)})

    def _literal(self, unit: Unit | None) -> Node | None:
        if unit is None:
            return None
        place = self.place_of([unit])
        return Node("FlowchartLiteral", place, {"kind": None, "value": self.text_of(place)})

    def parse_description_block(self) -> Node:
        start = self.next
        self.take("accDescr")
        self.take("{")
        text = self.take_if("description-block-text")
        closing = None if self.peek() is None else self.take("}")
        return Node("FlowchartAccDescrBlockStatement", self.place_from(start),
                    {"value": self._literal(text), "closed": closing is not None})


@dataclass
class Diagram:
    """What the adapter reads from a parsed source."""
    kind: str  # 'flowchart' or 'other'
    body: Node | None
    has_directives: bool
    has_frontmatter: bool
    source: Source

    def location(self, node_or_range) -> tuple[int, int]:
        start = node_or_range.range[0] if isinstance(node_or_range, Node) else node_or_range[0]
        return self.source.location(start)


def parse(text: str) -> Diagram:
    """Parse Mermaid source. Raises :class:`ParseError` for rejected input."""
    source = Source(text)
    kind, position = detect(source)
    if kind != "flowchart":
        return Diagram(kind, None, source.has_directives, source.has_frontmatter, source)
    chars = read_diagram_text(source, position)
    units = FlowchartLexer(chars, source).lex()
    body = FlowchartGrammar(units, source).parse_diagram()
    return Diagram(kind, body, source.has_directives, source.has_frontmatter, source)
