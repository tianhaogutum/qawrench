from __future__ import annotations

import json
import math

import pytest

from qawrench.statewatch.policy import Policy, PolicyError
from qawrench.statewatch.state import clone_json


@pytest.mark.parametrize(
    "data,match",
    [
        ({}, "select at least"),
        ([], "JSON object"),
        ({"env": "*"}, "list"),
        ({"env": [2]}, "list"),
        ({"cwd": "true"}, "true or false"),
        ({"cwd": True, "typo": []}, "unknown"),
        ({"variables": ["module:run()"]}, "invalid variable"),
        ({"variables": ["module:STATE[0]"]}, "invalid variable"),
        ({"cwd": True, "max_value_bytes": True}, "integer"),
        ({"cwd": True, "max_value_bytes": 10}, "integer"),
        ({"cwd": True, "max_value_bytes": 1048577}, "integer"),
    ],
)
def test_invalid_policy(tmp_path, data, match):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data))
    with pytest.raises(PolicyError, match=match):
        Policy.load(path)


def test_duplicate_policy_key_is_rejected(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text('{"cwd": true, "cwd": false}')
    with pytest.raises(PolicyError, match="duplicate"):
        Policy.load(path)


@pytest.mark.parametrize(
    "value,match",
    [
        ({1: "numeric key"}, "keys must be strings"),
        ((1, 2), "unsupported tuple"),
        (math.nan, "Out of range float"),
        ("a" * 200, "exceeds 128"),
    ],
)
def test_unrepresentable_values(value, match):
    with pytest.raises((ValueError, TypeError), match=match):
        clone_json(value, 128)


def test_cycle_and_snapshot_detachment():
    value = {"cache": [1]}
    cloned = clone_json(value, 128)
    value["cache"].append(2)
    assert cloned == {"cache": [1]}
    value["cycle"] = value
    with pytest.raises(ValueError, match="cyclic"):
        clone_json(value, 1024)


def test_deeply_nested_policy_is_an_actionable_error(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text("[" * 10000 + "0" + "]" * 10000)
    with pytest.raises(PolicyError):
        Policy.load(path)


@pytest.mark.parametrize("limit", [0, True, 1023, 8388609, "1024"])
def test_invalid_snapshot_budget(tmp_path, limit):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"cwd": True, "max_snapshot_bytes": limit}))
    with pytest.raises(PolicyError, match="max_snapshot_bytes"):
        Policy.load(path)
