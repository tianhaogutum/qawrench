"""Bridge existing discovery reports into explicit state selection; never import a suite."""

from __future__ import annotations

import json
import sys
from glob import escape
from pathlib import Path


def policy_from_lanes(path: Path, modules: list[str]) -> tuple[dict, list[str], bool]:
    """Read pytest-threadlanes 0.1.0's verified shared-state JSON shape.

    Include *all* severities: a thread-safe 'set-once' cache can still pollute a
    later test. Explicit module prefixes avoid guessing where a module name ends
    and a class/attribute path begins. Whole globals are selected for JSON diffs.
    """
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("discovery report exceeds 16 MiB; narrow upstream discovery first")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except RecursionError as exc:
        raise ValueError("discovery report nesting exceeds parser limits") from exc
    if (
        not isinstance(report, dict)
        or not isinstance(report.get("findings"), list)
        or type(report.get("tests")) is not int
        or type(report.get("truncated")) is not bool
    ):
        raise ValueError("not a pytest-threadlanes 0.1 shared-state report")
    if any(
        not module or not all(p.isidentifier() for p in module.split(".")) for module in modules
    ):
        raise ValueError("--module must be a dotted Python module name")
    env, variables, unmapped = set(), set(), []
    cwd = False
    for finding in report["findings"]:
        if not isinstance(finding, dict) or not isinstance(finding.get("path"), str):
            raise ValueError("invalid finding path in shared-state report")
        source = finding["path"]
        if source.startswith("env:") and source[4:]:
            env.add(escape(source[4:]))
        elif source == "cwd":
            cwd = True
        elif source == "sys.path":
            variables.add("sys:path")
        elif source == "logging:root.level":
            variables.add("logging:root.level")
        elif source.startswith("module:"):
            name = source[len("module:") :]
            matched = False
            for module in sorted(modules, key=len, reverse=True):
                if not name.startswith(module + "."):
                    continue
                attribute = name[len(module) + 1 :].split("[", 1)[0].split(".", 1)[0]
                if attribute.isidentifier():
                    variables.add(f"{module}:{attribute}")
                    matched = True
                    break
            if not matched:
                unmapped.append(source)
        else:
            unmapped.append(source)
    policy = {
        "env": sorted(env),
        "cwd": cwd,
        "variables": sorted(variables),
        "allow": [],
        "redact": ["env:*", "var:*"],
    }
    return policy, unmapped, report["truncated"]


def doctor_argv(path: Path, policy: Path) -> list[str]:
    """Attach value observation to an upstream verified order repro, without re-diagnosing it."""
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("doctor report exceeds 1 MiB")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except RecursionError as exc:
        raise ValueError("doctor report nesting exceeds parser limits") from exc
    if not isinstance(report, dict):
        raise ValueError("not a flakedoctor report object")
    repro = report.get("repro")
    if (
        report.get("format") != "flakedoctor-report"
        or report.get("version") != 1
        or report.get("verdict") != "flaky-order"
        or report.get("claim") != "observed"
        or not isinstance(repro, dict)
        or repro.get("axis") != "order"
    ):
        raise ValueError("requires an observed flakedoctor v1 order reproduction")
    polluters, victim, blob = repro.get("polluters"), report.get("nodeid"), repro.get("blob")
    if (
        not isinstance(polluters, list)
        or not polluters
        or any(type(nodeid) is not str or not nodeid for nodeid in polluters)
        or type(victim) is not str
        or not victim
        or type(blob) is not str
        or not blob.startswith("fd1:")
    ):
        raise ValueError("invalid structured order reproduction")
    return [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "statewatch",
        "-p",
        "flakedoctor",
        f"--doctor-repro={blob}",
        f"--statewatch={policy.resolve()}",
        *polluters,
        victim,
    ]
