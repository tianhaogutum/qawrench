"""Offline accessibility report interoperability; never runs a scanner."""
from .core import BridgeError, compare, make_baseline, validate_baseline, validate_exceptions
from .catalog import validate_catalog
from .draft import draft_exception
from .migration import diagnose_migration, validate_migration
from .sarif import migration_to_sarif, to_sarif

from qawrench import __version__
__all__ = ["BridgeError", "compare", "make_baseline", "validate_baseline", "validate_exceptions", "to_sarif", "migration_to_sarif", "diagnose_migration", "validate_migration", "draft_exception", "validate_catalog"]
