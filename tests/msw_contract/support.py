"""Shared paths, Node/npm detection and a synthetic runner for the msw-contract tests.

Tests that execute real MSW handlers need Node.js 24+ and the npm packages listed in
``fixtures/package.json`` (``npm install --prefix tests/msw_contract/fixtures --ignore-scripts``).
They are skipped cleanly otherwise. Everything else feeds synthetic runner output to
the Python OpenAPI checks, so it needs neither Node nor msw.
"""
from __future__ import annotations

import copy
import functools
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
UPSTREAM = FIXTURES / "upstream"
CONTRACT = FIXTURES / "contract-3.0.yaml"
HANDLERS = FIXTURES / "handlers.mjs"
EXAMPLES = ROOT / "examples" / "msw-contract"


@functools.cache
def node_major() -> int | None:
    node = shutil.which("node")
    if not node:
        return None
    try:
        version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.match(r"v(\d+)\.", version)
    return int(match.group(1)) if match else None


def npm_package_installed(name: str, start: Path = FIXTURES) -> bool:
    """Whether Node would find ``name`` in a node_modules folder at or above ``start``."""
    return any((directory / "node_modules" / name / "package.json").is_file()
               for directory in (start, *start.parents))


def _skip_reason(packages: tuple[str, ...]) -> str | None:
    major = node_major()
    if major is None:
        return "node is not on PATH"
    if major < 24:
        return f"Node.js 24 or newer is required (found {major})"
    missing = [name for name in ("msw", *packages) if not npm_package_installed(name)]
    if missing:
        return (f"npm packages {', '.join(missing)} are not installed for tests/msw_contract/fixtures "
                "(npm install --prefix tests/msw_contract/fixtures --ignore-scripts)")
    return None


def requires_node(*packages: str):
    """Skip unless Node.js 24+ and ``msw`` (plus ``packages``) resolve from the fixtures directory."""
    reason = _skip_reason(packages)
    return pytest.mark.skipif(reason is not None, reason=reason or "")


requires_msw = requires_node()
requires_node_only = pytest.mark.skipif(node_major() is None or node_major() < 24,
                                        reason="Node.js 24 or newer is not on PATH")


def run_cli(*args, cwd: Path = ROOT, env: dict | None = None, timeout: float = 120):
    return subprocess.run([sys.executable, "-m", "qawrench", "msw-contract", *map(str, args)], cwd=cwd,
                          capture_output=True, text=True, timeout=timeout, env=env)


def example(scenario: str, **extra) -> dict:
    return {"id": scenario, "url": f"http://example.test/pets/1?scenario={scenario}", **extra}


# ---------------------------------------------------------------------------
# Synthetic runner output (what node/runner.mjs reports for a case)


def response(body: str = "", *, status: int = 200, content_type: str | None = "application/json",
             headers: dict | None = None) -> dict:
    all_headers = dict(headers or {})
    if content_type is not None:
        all_headers["content-type"] = content_type
    return {"response": {"status": status, "headers": all_headers, "body": body,
                         "bytes": len(body.encode("utf-8"))}}


def json_response(value, **options) -> dict:
    return response(json.dumps(value, separators=(",", ":"), ensure_ascii=False), **options)


def error_result(code: str, message: str = "synthetic failure", diagnostics: str | None = None) -> dict:
    result = {"error": {"code": code, "message": message}}
    return {**result, "diagnostics": diagnostics} if diagnostics else result


# Equivalents of fixtures/handlers.mjs branches, keyed by case id.
SCENARIOS = {
    "ok": json_response({"id": 1, "name": "Milo"}),
    "stale": json_response({"id": "1", "name": "Milo"}),
    "missing": json_response({"id": 1}),
    "additional": json_response({"id": 1, "name": "Milo", "oldField": True}),
    "status": json_response({"id": 1, "name": "Milo"}, status=418),
    "media": response("wrong content type", content_type="text/plain"),
    "missing-type": response('{"id":1}', content_type=""),
    "malformed": response("{not json"),
    "nullable": json_response({"id": 1, "name": None}),
    "isolation": json_response({"id": 1, "name": "Milo"}),
    "isolation-again": json_response({"id": 1, "name": "Milo"}),
    "post": json_response({"name": "Milo", "id": 1}),
    "unicode": json_response({"id": 1, "name": "😀😀😀😀"}),
    "problem": json_response({"title": 42}, status=400, content_type="application/problem+json; charset=utf-8"),
    "text": response("hello", content_type="text/plain"),
    "empty": response("", status=204, content_type=None),
}


class FakeRunner:
    """Stands in for ``NodeRunner``; returns prepared results by case id."""

    results: dict = {}
    seen: list = []

    def __init__(self, handlers_path, *, cwd, node=None):
        self.handlers_path = handlers_path

    def start(self, cancel=None):
        return {"type": "ready"}

    def run(self, example, **options):
        FakeRunner.seen.append((example, options))
        return copy.deepcopy(FakeRunner.results[example["id"]])

    def close(self):
        pass


@pytest.fixture
def fake_runner(monkeypatch):
    from qawrench.msw_contract import core

    FakeRunner.results = dict(SCENARIOS)
    FakeRunner.seen = []
    monkeypatch.setattr(core, "NodeRunner", FakeRunner)
    return FakeRunner
