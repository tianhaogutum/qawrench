"""``qawrench check``: one read-only pass over a folder of CI test artifacts.

``check`` finds Playwright JSON and CTRF reports, Playwright traces, HAR
recordings, pytest statewatch reports, axe and Pa11y CI results, Lighthouse
JSON reports, lychee JSON and Parquet export manifests by their content, then
runs the read-only analyses of the other qawrench tools on them (flaky, har,
trace-budget, statewatch, a11y, lighthouse, links, crawlee-export). It never
writes a file, never executes anything from the artifacts and never opens a
network connection.

Library use::

    from qawrench.check import CheckOptions, discover, run_checks
    discovery = discover(["test-results/"])
    findings = run_checks(discovery, options=CheckOptions(trace_budget=25_000_000))
"""
from __future__ import annotations

from qawrench import __version__
from .discover import Discovery, DiscoveryError, Source, discover
from .model import SEVERITIES, Finding
from .registry import AREAS, CHECKS, CheckOptions, run_checks, select

__all__ = [
    "AREAS",
    "CHECKS",
    "SEVERITIES",
    "CheckOptions",
    "Discovery",
    "DiscoveryError",
    "Finding",
    "Source",
    "__version__",
    "discover",
    "run_checks",
    "select",
]
