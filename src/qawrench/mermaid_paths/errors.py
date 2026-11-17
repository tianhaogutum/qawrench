"""The single error type of the adapter, with a stable machine-readable code."""
from __future__ import annotations

from typing import NoReturn


class AppendixError(Exception):
    """An input, option or review problem; ``code`` names the failure (for example ``STALE_REVIEW``)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message


def fail(code: str, message: str) -> NoReturn:
    raise AppendixError(code, message)


def at(loc: tuple[int, int] | None) -> str:
    """`` at LINE:COLUMN`` for a (1-based line, 0-based UTF-16 column) location."""
    return f" at {loc[0]}:{loc[1] + 1}" if loc is not None else ""
