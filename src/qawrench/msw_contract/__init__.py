"""Execute explicit request cases with real MSW handlers and audit the actual responses against OpenAPI.

Handlers are JavaScript and run in the bundled Node runner (``node/runner.mjs``);
loading OpenAPI documents, routing, schema validation and reporting are Python.
``check`` and ``format_text`` mirror the original JavaScript ``check``/``formatText``
API. Loading contracts needs the ``openapi`` extra (PyYAML and jsonschema).
"""
from qawrench import __version__

from ._js import utf8_prefix
from .contract import check_response, load_contract, select_media
from .core import check, format_text, write_report
from .errors import CheckCancelled, CheckError, RuntimeUnavailable
from .inputs import normalize_cases, read_data
from .runner import run_handler

__all__ = [
    "CheckCancelled", "CheckError", "RuntimeUnavailable", "check", "check_response", "format_text",
    "load_contract", "normalize_cases", "read_data", "run_handler", "select_media", "utf8_prefix",
    "write_report", "__version__",
]
