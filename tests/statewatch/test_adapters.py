from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from qawrench.statewatch.adapters import doctor_argv, policy_from_lanes
from qawrench.statewatch.policy import Policy

from .conftest import FIXTURES

# Unmodified producer reports from the pytest-leak-finder demo (see fixtures/).
LANES_REPORT = FIXTURES / "lanes-upstream-report.json"


def test_real_upstream_discovery_maps_set_once_findings_too(tmp_path):
    policy, unmapped, truncated = policy_from_lanes(LANES_REPORT, ["test_demo"])
    assert policy["variables"] == ["test_demo:leak_state"]
    assert unmapped == []
    assert not truncated
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    assert Policy.load(path).variables == ("test_demo:leak_state",)


def test_real_discovery_to_value_diff_and_replay(tmp_path):
    policy, _, _ = policy_from_lanes(LANES_REPORT, ["test_demo"])
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    output = tmp_path / "observed"
    # The upstream demo keeps its module name, which the discovery report refers to.
    suite = tmp_path / "suite"
    suite.mkdir()
    shutil.copy(FIXTURES / "leak_finder_demo.py", suite / "test_demo.py")
    (suite / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "statewatch",
            "test_demo.py",
            "-q",
            f"--statewatch={path}",
            f"--statewatch-output={output}",
            "--statewatch-confirm",
            f"--basetemp={tmp_path / 'pytest-tmp'}",
        ],
        cwd=suite,
        env=dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", TMPDIR=str(tmp_path)),
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads((output / "report.json").read_text())
    assert report["confirmations"][0]["status"] == "pair_reproduced"


def test_ambiguous_module_paths_require_a_prefix():
    policy, unmapped, _ = policy_from_lanes(LANES_REPORT, [])
    assert policy["variables"] == []
    assert unmapped == ["module:test_demo.leak_state"]


def test_invalid_upstream_shape_is_not_silently_accepted(tmp_path):
    path = tmp_path / "wrong.json"
    path.write_text('{"findings": []}')
    with pytest.raises(ValueError, match="not a pytest-threadlanes"):
        policy_from_lanes(path, [])


def test_real_doctor_order_report_preserves_the_upstream_repro_blob(tmp_path):
    path = FIXTURES / "doctor-order-report.json"
    source = json.loads(path.read_text())
    command = doctor_argv(path, tmp_path / "policy.json")
    assert command[-2:] == ["test_demo.py::test3", "test_demo.py::test5"]
    assert f"--doctor-repro={source['repro']['blob']}" in command
    assert "--statewatch-confirm" not in command


def test_incomplete_real_doctor_report_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="requires an observed"):
        doctor_argv(FIXTURES / "doctor-upstream-report.json", tmp_path / "policy.json")


def test_oversized_discovery_is_rejected_before_loading(tmp_path):
    path = tmp_path / "too-large.json"
    with path.open("wb") as handle:
        handle.truncate(16 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="exceeds 16 MiB"):
        policy_from_lanes(path, [])


def test_deeply_nested_discovery_report_is_rejected_cleanly(tmp_path):
    path = tmp_path / "deep.json"
    path.write_text("[" * 10000 + "0" + "]" * 10000)
    with pytest.raises(ValueError, match="nesting exceeds parser limits"):
        policy_from_lanes(path, [])


def test_deeply_nested_doctor_report_is_rejected_cleanly(tmp_path):
    path = tmp_path / "deep-doctor.json"
    path.write_text("[" * 10000 + "0" + "]" * 10000)
    with pytest.raises(ValueError, match="nesting exceeds parser limits"):
        doctor_argv(path, tmp_path / "policy.json")
