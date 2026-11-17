"""Bounded JSON export, formatted exactly like ``JSON.stringify(data, null, 2) + "\\n"``."""
from __future__ import annotations

import json
from typing import Any

from .errors import fail

MAX_JSON_BYTES = 16_777_216


def _dumps(value: Any, **kwargs) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, **kwargs)


def json_output(data: Any) -> str:
    """Bound exported data before formatting the full string."""
    estimate = 0
    pending = [data]
    while pending:
        value = pending.pop()
        # More than the indentation/punctuation needed by these shallow schemas.
        estimate += 64
        if isinstance(value, str):
            estimate += len(_dumps(value).encode("utf-8", "surrogatepass"))
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, dict):
            for key, child in value.items():
                estimate += len(_dumps(key).encode("utf-8", "surrogatepass"))
                pending.append(child)
        if estimate > MAX_JSON_BYTES:
            fail("OUTPUT_LIMIT", "Expanded JSON exceeds the 16 MiB safety budget; split the diagram or "
                 "shorten repeated identifiers/labels.")
    result = _dumps(data, indent=2) + "\n"
    if len(result.encode("utf-8", "surrogatepass")) > MAX_JSON_BYTES:
        fail("OUTPUT_LIMIT", "JSON output exceeds 16 MiB; split the diagram.")
    return result
