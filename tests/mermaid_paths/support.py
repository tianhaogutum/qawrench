"""Shared paths, a CLI runner, an HTML tree helper and a structural HTML checker.

The original project validated generated HTML with html-validate (npm). qawrench's
tests stay standard-library only, so :func:`html_problems` checks the structural
rules that matter for these appendices instead: balanced nesting, permitted
children of lists/tables/paragraphs, unique IDs, resolvable in-page links and
``aria-labelledby`` references, heading order, ``th`` scopes, valid ``lang``
values, no duplicate attributes, inline styles or scripts.
"""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import subprocess
import sys

import pytest

from qawrench.mermaid_paths import AppendixError, apply_review, create_review
from qawrench.mermaid_paths.i18n import language_tag

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
UPSTREAM = FIXTURES / "upstream"
EXAMPLES = ROOT / "examples" / "mermaid-paths"
SAMPLE = (EXAMPLES / "review-process.mmd").read_text(encoding="utf-8")

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
BLOCK_IN_P = {"address", "article", "aside", "blockquote", "div", "dl", "fieldset", "footer", "form", "h1", "h2",
              "h3", "h4", "h5", "h6", "header", "hr", "main", "nav", "ol", "p", "pre", "section", "table", "ul"}
ALLOWED_CHILDREN = {
    "ul": {"li"}, "ol": {"li"}, "table": {"caption", "thead", "tbody", "tfoot", "tr", "colgroup"},
    "thead": {"tr"}, "tbody": {"tr"}, "tr": {"th", "td"},
}


def run(*args, input: bytes | str | None = None):
    """Run ``qawrench mermaid-paths`` in a subprocess; returns (status, stdout, stderr) as text."""
    if isinstance(input, str):
        input = input.encode("utf-8")
    result = subprocess.run([sys.executable, "-m", "qawrench", "mermaid-paths", *map(str, args)], input=input,
                            capture_output=True, timeout=60)
    return result.returncode, result.stdout.decode("utf-8", "replace"), result.stderr.decode("utf-8", "replace")


def confirmed(graph: dict) -> dict:
    review = create_review(graph)
    for edge in review["edges"]:
        edge["confirmed"] = True
    return apply_review(graph, review)


def raises(code: str, function, *args, **kwargs) -> AppendixError:
    with pytest.raises(AppendixError) as info:
        function(*args, **kwargs)
    assert info.value.code == code, f"{info.value.code}: {info.value}"
    return info.value


class Element:
    def __init__(self, tag: str, attrs: list[tuple[str, str | None]], parent: "Element | None"):
        self.tag = tag
        self.attr_list = attrs
        self.attrs = {name: (value if value is not None else "") for name, value in attrs}
        self.parent = parent
        self.children: list[Element] = []


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Element("#root", [], None)
        self.current = self.root
        self.elements: list[Element] = []
        self.problems: list[str] = []
        self.doctype = False

    def handle_decl(self, decl):
        if decl.lower() == "doctype html":
            self.doctype = True

    def handle_starttag(self, tag, attrs):
        element = Element(tag, attrs, self.current)
        self.current.children.append(element)
        self.elements.append(element)
        if tag not in VOID:
            self.current = element

    def handle_startendtag(self, tag, attrs):
        element = Element(tag, attrs, self.current)
        self.current.children.append(element)
        self.elements.append(element)
        if tag not in VOID:
            self.problems.append(f"self-closing non-void element <{tag}/>")

    def handle_endtag(self, tag):
        if tag in VOID:
            self.problems.append(f"end tag for void element </{tag}>")
            return
        if self.current.tag != tag:
            self.problems.append(f"</{tag}> closes <{self.current.tag}>")
            return
        self.current = self.current.parent


def tree(html: str) -> list[Element]:
    """Every element of ``html`` in document order (a parse5 ``descendants`` stand-in)."""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.elements


def html_problems(html: str, document: bool = True) -> list[str]:
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    problems = list(builder.problems)
    if builder.current is not builder.root:
        problems.append(f"unclosed <{builder.current.tag}>")
    elements = builder.elements
    if document:
        if not builder.doctype:
            problems.append("missing <!DOCTYPE html>")
        html_elements = [e for e in elements if e.tag == "html"]
        if len(html_elements) != 1 or not html_elements[0].attrs.get("lang"):
            problems.append("document needs one <html lang>")
        titles = [e for e in elements if e.tag == "title"]
        if len(titles) != 1:
            problems.append("document needs exactly one <title>")
    ids = [e.attrs["id"] for e in elements if "id" in e.attrs]
    if len(ids) != len(set(ids)):
        problems.append("duplicate id")
    known = set(ids)
    previous_level = 0
    for element in elements:
        names = [name for name, _ in element.attr_list]
        if len(names) != len(set(names)):
            problems.append(f"duplicate attribute on <{element.tag}>")
        if element.tag == "script" or "style" in element.attrs or any(n.startswith("on") for n in names):
            problems.append(f"script or inline style on <{element.tag}>")
        if element.tag == "a":
            href = element.attrs.get("href", "")
            if not href.startswith("#") or href[1:] not in known:
                problems.append(f"unresolved link {href!r}")
        if "aria-labelledby" in element.attrs and element.attrs["aria-labelledby"] not in known:
            problems.append("unresolved aria-labelledby")
        if "lang" in element.attrs:
            try:
                language_tag(element.attrs["lang"])
            except AppendixError:
                problems.append(f"invalid lang {element.attrs['lang']!r}")
        if element.tag == "th" and element.attrs.get("scope") not in ("col", "row"):
            problems.append("th without scope")
        allowed = ALLOWED_CHILDREN.get(element.tag)
        if allowed is not None:
            for child in element.children:
                if child.tag not in allowed:
                    problems.append(f"<{child.tag}> not permitted in <{element.tag}>")
        if element.tag == "p":
            for child in element.children:
                if child.tag in BLOCK_IN_P:
                    problems.append(f"<{child.tag}> not permitted in <p>")
        if len(element.tag) == 2 and element.tag[0] == "h" and element.tag[1].isdigit():
            level = int(element.tag[1])
            if level > previous_level + 1:
                problems.append(f"heading level skips from h{previous_level} to h{level}")
            previous_level = level
    return problems
