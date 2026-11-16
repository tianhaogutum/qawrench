"""Offline structured metadata parity, backed by extruct 0.18.0."""

from qawrench import __version__

from .core import ParityError, compare_files
from .baseline import make_baseline

__all__ = ["ParityError", "compare_files", "make_baseline"]
