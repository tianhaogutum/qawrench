from __future__ import annotations

import pytest


def test_rerunfailures_protocol_can_explain_a_following_victim(suite):
    pytest.importorskip("pytest_rerunfailures")
    source = """
STATE = 0
def test_retry():
    global STATE
    STATE += 1
    assert STATE >= 2
def test_victim(): assert STATE == 0
"""
    result, report, events, _ = suite(
        source,
        {"variables": ["test_subject:STATE"]},
        args=(
            "-p",
            "rerunfailures",
            "--reruns=1",
            "--statewatch-confirm",
        ),
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert len(events) == 2
    assert any(phase["outcome"] == "rerun" for phase in events[0]["reports"])
    assert report["first_observed_writers"]["var:test_subject:STATE"] == (
        "test_subject.py::test_retry"
    )
    assert report["confirmations"][0]["status"] == "pair_reproduced"


def test_rerunfailures_monkeypatch_cleanup_is_not_a_leak(suite):
    pytest.importorskip("pytest_rerunfailures")
    source = """
import os
ATTEMPTS = 0
def test_retry(monkeypatch):
    global ATTEMPTS
    ATTEMPTS += 1
    monkeypatch.setenv("STATEWATCH_TEST_ATTEMPT", "temporary")
    assert ATTEMPTS >= 2
def test_following():
    value = os.environ.get("STATEWATCH_TEST_ATTEMPT")
    assert value is None
"""
    result, report, events, _ = suite(
        source,
        {"env": ["STATEWATCH_TEST_*"]},
        args=(
            "-p",
            "rerunfailures",
            "--reruns=1",
        ),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert report["tests_with_leaks"] == 0
    assert len(events[0]["phase_changes"]) == 4


def test_xdist_is_rejected_before_workers_or_artifacts_start(suite):
    pytest.importorskip("xdist")
    result, report, events, root = suite(
        "def test_ok(): pass\n",
        {"cwd": True},
        args=(
            "-p",
            "xdist",
            "-n",
            "2",
        ),
    )
    assert result.returncode == 4
    assert "one process" in result.stderr
    assert report is None
    assert not events
    assert not (root / "artifacts").exists()


def test_randomly_and_replay_compose_without_duplicate_registration(suite):
    pytest.importorskip("pytest_randomly")
    source = """
import os
def test_writer(): os.environ["STATEWATCH_TEST_VALUE"] = "dirty"
def test_victim():
    value = os.environ.get("STATEWATCH_TEST_VALUE")
    assert value is None
"""
    result, report, _, _ = suite(
        source,
        {"env": ["STATEWATCH_TEST_*"]},
        args=(
            "-p",
            "randomly",
            "--randomly-dont-reorganize",
            "--randomly-seed=42",
            "--statewatch-confirm",
        ),
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert report["confirmations"][0]["status"] == "pair_reproduced"


def test_exact_duplicate_nodeids_are_inconclusive_for_upstream_replay(suite):
    source = """
STATE = 0
def test_writer():
    global STATE
    STATE += 1
def test_victim(): assert STATE == 0
"""
    result, report, events, _ = suite(
        source,
        {"variables": ["test_subject:STATE"]},
        args=(
            "--keep-duplicates",
            "test_subject.py",
            "--statewatch-confirm",
        ),
    )
    assert result.returncode == 1
    assert len(events) == 4
    assert report["duplicate_nodeids"] == [
        "test_subject.py::test_victim",
        "test_subject.py::test_writer",
    ]
    assert all(c["status"] == "duplicate_nodeids" for c in report["confirmations"])
    assert all(c["baseline"] is None for c in report["confirmations"])


def test_exhausted_budget_does_not_erase_a_reproduced_pair(suite):
    source = """
STATE = {"a": 0, "b": 0}
OTHER = 0
def test_first(): STATE["a"] = 1
def test_second():
    global OTHER
    OTHER = 1
def test_victim(): assert STATE["a"] == 0
"""
    result, report, _, root = suite(
        source,
        {"variables": ["test_subject:STATE", "test_subject:OTHER"]},
        args=("--statewatch-confirm", "--statewatch-max-replays=2"),
    )
    assert result.returncode == 1
    entry = report["confirmations"][0]
    assert entry["status"] == "pair_reproduced"
    assert entry["budget_exhausted"]
    assert len(list((root / "artifacts/replays").glob("*/*.jsonl"))) == 2
