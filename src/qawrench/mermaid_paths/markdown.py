"""Mermaid code fences in Markdown documents, found with Marked's block rules."""
from __future__ import annotations

import re

from ._js import JS_WHITESPACE
from ._marked import BlockLexer, walk_tokens
from .diagram import assert_source_budget, source_hash
from .errors import AppendixError, fail

MAX_SAFE_INTEGER = 2 ** 53 - 1
_JS_SPACE = re.compile("[" + re.escape(JS_WHITESPACE) + "]")


def extract_mermaid_blocks(markdown: str) -> list[dict]:
    """Code fences with a Mermaid language tag, in document order (including nested lists/quotes)."""
    assert_source_budget(markdown)
    blocks = []
    try:
        for token in walk_tokens(BlockLexer().lex(markdown)):
            lang = token.get("lang") if token["type"] == "code" else None
            if lang is None or _JS_SPACE.split(lang.strip(JS_WHITESPACE))[0].lower() != "mermaid":
                continue
            # Marked supplies de-indented, LF-normalized diagram text. Add a terminal
            # LF so equivalent fence/line-ending styles extract consistently.
            source = token["text"] + "\n"
            blocks.append({"index": len(blocks) + 1, "source": source, "sourceSha256": source_hash(source)})
    except AppendixError:
        raise
    except (ValueError, RecursionError, re.error) as error:
        fail("MARKDOWN_PARSE", f"Markdown structure could not be processed: {str(error)[:300]}")
    return blocks


def select_mermaid_block(markdown: str, diagram=None) -> dict:
    """The one Mermaid block, or the 1-based ``diagram`` of several; returns ``{index, source, sourceSha256}``."""
    if diagram is not None and (isinstance(diagram, bool) or not isinstance(diagram, int)
                                or not 1 <= diagram <= MAX_SAFE_INTEGER):
        fail("DIAGRAM_SELECTION", "Diagram number must be a positive integer (starting at 1).")
    blocks = extract_mermaid_blocks(markdown)
    if not blocks:
        fail("NO_DIAGRAM", "No Mermaid code fences were found in this Markdown document.")
    if diagram is None and len(blocks) > 1:
        fail("DIAGRAM_SELECTION", f"Markdown contains {len(blocks)} Mermaid diagrams; choose --diagram 1 through "
             f"{len(blocks)}.")
    if diagram is not None and diagram > len(blocks):
        fail("DIAGRAM_SELECTION", f"Requested diagram {diagram}, but Markdown contains only {len(blocks)} Mermaid "
             "diagram(s).")
    return blocks[(diagram or 1) - 1]
