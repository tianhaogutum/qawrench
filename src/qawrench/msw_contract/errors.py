"""Exceptions raised before or while executing explicit request cases."""
from __future__ import annotations


class CheckError(Exception):
    """Invalid input or configuration; the CLI reports it on one line and exits 2."""


class CheckCancelled(CheckError):
    """The caller's cancel event was set; active Node and validator processes were stopped."""


class RuntimeUnavailable(CheckError):
    """Node.js is missing or too old, or the project's ``msw`` cannot be resolved."""
