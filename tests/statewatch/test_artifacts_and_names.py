from __future__ import annotations


def test_unicode_python_identifiers_can_be_declared(suite):
    source = """
import 状态
def test_writer(): 状态.配置["缓存"].append("污染")
def test_victim(): assert 状态.配置["缓存"] == []
"""
    result, report, _, _ = suite(
        source,
        {"variables": ["状态:配置"]},
        files={"状态.py": '配置 = {"缓存": []}\n'},
        args=("--statewatch-confirm",),
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert report["first_observed_writers"]["var:状态:配置"] == "test_subject.py::test_writer"
    assert report["confirmations"][0]["status"] == "pair_reproduced"


def test_existing_event_symlink_does_not_truncate_its_target(suite, tmp_path):
    output = tmp_path / "artifacts"
    output.mkdir()
    target = tmp_path / "important.txt"
    target.write_text("preserve this file")
    (output / "events.jsonl").symlink_to(target)
    result, report, _, _ = suite("def test_ok(): pass\n", {"cwd": True})
    assert result.returncode == 0
    assert target.read_text() == "preserve this file"
    assert not (output / "events.jsonl").is_symlink()
    assert report is not None


def test_confirmation_keeps_the_effective_policy_if_input_file_changes(suite):
    source = """
import os
from pathlib import Path
def test_writer():
    os.environ["STATEWATCH_TEST_VALUE"] = "dirty"
    Path("policy.json").write_text("{}")
def test_victim():
    value = os.environ.get("STATEWATCH_TEST_VALUE")
    assert value is None
"""
    result, report, _, _ = suite(
        source,
        {"env": ["STATEWATCH_TEST_*"]},
        args=("--statewatch-confirm",),
    )
    assert result.returncode == 1
    assert report["confirmations"][0]["status"] == "pair_reproduced"


def test_symlinked_lock_is_rejected_without_truncating_target(suite, tmp_path):
    output = tmp_path / "artifacts"
    output.mkdir()
    target = tmp_path / "important-lock-target.txt"
    target.write_text("preserve this file too")
    (output / ".statewatch.lock").symlink_to(target)
    result, _, _, _ = suite("def test_ok(): pass\n", {"cwd": True})
    assert result.returncode == 4
    assert target.read_text() == "preserve this file too"
