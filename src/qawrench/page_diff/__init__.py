"""Offline HTML comparison. See docs/page-diff.md for the version-1 rule contract."""
from qawrench import __version__

from .core import ContractError, compare_files, compare_html, load_rules, validate_rules

__all__ = ["ContractError", "compare_files", "compare_html", "load_rules", "validate_rules"]
