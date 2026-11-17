from __future__ import annotations

import json

import pytest

SOURCE = """
import os
def test_writer(): os.environ["STATEWATCH_TEST_VALUE"] = "dirty"
def test_victim():
    value = os.environ.get("STATEWATCH_TEST_VALUE")
    assert value is None
"""


@pytest.mark.parametrize("origin", ["ini", "environment"])
def test_confirmation_options_in_addopts_do_not_reactivate_children(suite, origin):
    options = "--statewatch-confirm --statewatch-fail-on-leak"
    files = {"pytest.ini": f"[pytest]\naddopts = {options}\n"} if origin == "ini" else None
    environment = {"PYTEST_ADDOPTS": options} if origin == "environment" else None
    result, report, _, _ = suite(
        SOURCE,
        {"env": ["STATEWATCH_TEST_*"]},
        files=files,
        environment=environment,
    )
    assert result.returncode == 1
    confirmation = report["confirmations"][0]
    assert confirmation["baseline"]["victim_outcome"] == "passed"
    assert confirmation["status"] == "pair_reproduced"


def test_restoring_signed_zero_uses_the_same_equivalence_as_deepdiff(suite):
    source = """
STATE = 0.0
def test_writer():
    global STATE
    STATE = 1.0
def test_restore():
    global STATE
    STATE = -0.0
def test_following(): pass
"""
    result, report, events, _ = suite(source, {"variables": ["test_subject:STATE"]})
    assert result.returncode == 0
    assert events[1]["net_changes"][0]["restored_to_baseline"]
    assert report["tests"][1]["leaked_fields"] == []
    assert events[2]["inherited"] == []


def test_unknown_origin_returning_to_baseline_starts_a_new_episode(suite):
    source = """
STATE = 0
def test_first():
    global STATE
    STATE = 1
def test_unreadable():
    global STATE
    STATE = object()
def test_restores():
    global STATE
    STATE = 0
def test_new_writer():
    global STATE
    STATE = 2
def test_victim(): assert STATE == 0
"""
    result, _, events, _ = suite(source, {"variables": ["test_subject:STATE"]})
    assert result.returncode == 1
    assert events[4]["inherited"][0]["first_writer"] == "test_subject.py::test_new_writer"


def test_unattributed_outside_protocol_leak_can_fail_ci(suite):
    conftest = """
import os
import pytest
class Outside:
    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        result = yield
        if item.name == "test_first": os.environ["STATEWATCH_TEST_VALUE"] = "outside"
        return result
def pytest_sessionstart(session): session.config.pluginmanager.register(Outside())
"""
    result, report, _, _ = suite(
        "def test_first(): pass\ndef test_following(): pass\n",
        {"env": ["STATEWATCH_TEST_*"]},
        conftest=conftest,
        args=("--statewatch-fail-on-leak",),
    )
    assert result.returncode == 1
    assert report["original_exit_code"] == 0
    assert report["unattributed_changes"][0]["field"] == "env:STATEWATCH_TEST_VALUE"


def test_user_nested_replay_confirmation_is_still_rejected(suite):
    result, report, _, _ = suite(
        "def test_ok(): pass\n",
        {"cwd": True},
        files={"record.jsonl": json.dumps({"nodeid": "test_subject.py::test_ok"}) + "\n"},
        args=("-p", "replay", "--replay=record.jsonl", "--statewatch-confirm"),
    )
    assert result.returncode == 4
    assert "cannot be nested" in result.stderr
    assert report is None


def test_unicode_and_shell_metacharacters_in_real_pytest_nodeids(suite):
    source = """
import os
import pytest
@pytest.mark.parametrize("value", [1], ids=["space quote';$(touch ESCAPED) 中文"])
def test_writer(value): os.environ["STATEWATCH_TEST_VALUE"] = "dirty"
def test_victim():
    value = os.environ.get("STATEWATCH_TEST_VALUE")
    assert value is None
"""
    result, report, _, root = suite(
        source,
        {"env": ["STATEWATCH_TEST_*"]},
        args=(
            "--statewatch-confirm",
            "-o",
            "disable_test_id_escaping_and_forfeit_all_rights_to_community_support=True",
            "--",
        ),
    )
    assert result.returncode == 1
    assert report["confirmations"][0]["status"] == "pair_reproduced"
    assert "中文" in report["confirmations"][0]["pairs"][0]["nodeids"][0]
    assert not (root / "ESCAPED").exists()


def test_deletion_is_distinct_from_none_and_restoration_is_not_a_leak(suite):
    source = """
import os
os.environ["STATEWATCH_TEST_EXISTING"] = "original"
STATE = None
def test_delete():
    global STATE
    del STATE
    del os.environ["STATEWATCH_TEST_EXISTING"]
def test_victim():
    value = os.environ.get("STATEWATCH_TEST_EXISTING")
    assert value == "original"
def test_restore():
    global STATE
    STATE = None
    os.environ["STATEWATCH_TEST_EXISTING"] = "original"
"""
    result, report, events, _ = suite(
        source,
        {"env": ["STATEWATCH_TEST_EXISTING"], "variables": ["test_subject:STATE"]},
        args=("--statewatch-confirm",),
    )
    assert result.returncode == 1
    variable = next(c for c in events[0]["net_changes"] if c["field"].startswith("var:"))
    assert variable["before"] == {"status": "value", "value": None}
    assert variable["after"] == {"status": "missing"}
    assert report["tests"][2]["leaked_fields"] == []
    assert report["confirmations"][0]["status"] == "pair_reproduced"
