"""Shared fixture: run pytest with the statewatch plugin in a fresh subprocess.

``-p statewatch`` loads the plugin through qawrench's ``pytest11`` entry point,
so these tests need qawrench installed (for example ``uv run --extra test``)
with the ``statewatch`` extra (DeepDiff, filelock, pytest-replay).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def suite(tmp_path):
    def run(
        source, policy=None, *, args=(), conftest=None, files=None, autoload=False, environment=None
    ):
        (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        (tmp_path / "test_subject.py").write_text(source, encoding="utf-8")
        if conftest:
            (tmp_path / "conftest.py").write_text(conftest, encoding="utf-8")
        for name, content in (files or {}).items():
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        env = dict(os.environ)
        temporary = tmp_path / "system-tmp"
        temporary.mkdir(exist_ok=True)
        env["TMPDIR"] = str(temporary)
        env.pop("PYTEST_ADDOPTS", None)
        env.pop("PYTEST_PLUGINS", None)
        if not autoload:
            env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        else:
            env.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
        for key in list(env):
            if key.startswith("STATEWATCH_TEST_"):
                del env[key]
        env.update(environment or {})
        command = [sys.executable, "-m", "pytest", "-p", "statewatch", "-q"]
        if autoload:
            command += ["-p", "no:randomly"]
        if policy is not None:
            (tmp_path / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
            command += ["--statewatch=policy.json", "--statewatch-output=artifacts"]
        command += [*args, "test_subject.py"]
        result = subprocess.run(
            command, cwd=tmp_path, env=env, text=True, capture_output=True, timeout=25
        )
        report_path = tmp_path / "artifacts/report.json"
        report = json.loads(report_path.read_text()) if report_path.exists() else None
        events_path = tmp_path / "artifacts/events.jsonl"
        events = (
            [json.loads(line) for line in events_path.read_text().splitlines()]
            if events_path.exists()
            else []
        )
        return result, report, events, tmp_path

    return run
