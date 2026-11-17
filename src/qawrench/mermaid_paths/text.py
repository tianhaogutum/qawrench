"""Label text, HTML escaping and plain-text validation."""
from __future__ import annotations

import re
from typing import Any

from . import _marked
from ._js import CONTROL, js_collapse, js_trim, utf16_len
from .errors import fail
from .htmltext import text_parts

_MERMAID_ENTITY = re.compile(r"(?<!&)#([a-zA-Z][a-zA-Z0-9]+|[0-9]+);")


def label_text(label: dict[str, Any] | None) -> str | None:
    """Convert an authored label (``{"kind", "value"}``) to readable text.

    Mermaid's ``#quot;``/``#9829;`` entities become HTML entities; Markdown labels
    are rendered to HTML first; HTML is then reduced to its readable text.
    """
    if not label:
        return None
    value = label["value"]
    if utf16_len(value) > 20_000:
        fail("LIMIT", "A label exceeds 20,000 characters.")
    entities = _MERMAID_ENTITY.sub(
        lambda m: f"&#{m.group(1)};" if m.group(1)[0].isdigit() else f"&{m.group(1)};", value)
    html = _marked.to_html(entities) if label.get("kind") == "markdown" else entities
    text = js_collapse("".join(text_parts(html)))
    if CONTROL.search(text):
        fail("INVALID_TEXT", "A decoded label contains unsupported control characters.")
    return text


def escape_html(value: Any) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def plain_text(value: Any, field: str, max_length: int = 2_000) -> str:
    if (not isinstance(value, str) or not js_trim(value) or utf16_len(value) > max_length
            or CONTROL.search(value)):
        fail("INVALID_TEXT", f"{field} must be nonempty text of at most {max_length} characters, "
             "without control characters.")
    return js_trim(value)
