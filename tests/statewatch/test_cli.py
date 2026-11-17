from __future__ import annotations

import json

from qawrench.statewatch.cli import main


def test_cli_rejects_non_object_report(tmp_path, capsys):
    path = tmp_path / "invalid.json"
    path.write_text("[]")
    assert main([str(path)]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_cli_reads_a_real_generated_report(suite, capsys):
    source = """
import os
def test_writer(): os.environ["STATEWATCH_TEST_VALUE"] = "yes"
def test_victim():
    value = os.environ.get("STATEWATCH_TEST_VALUE")
    assert value is None
"""
    result, _, _, root = suite(
        source,
        {"env": ["STATEWATCH_TEST_*"]},
        args=("--statewatch-confirm",),
    )
    assert result.returncode == 1
    assert main([str(root / "artifacts/report.json")]) == 0
    output = capsys.readouterr().out
    assert "first observed writer test_subject.py::test_writer" in output
    assert "pair_reproduced" in output
    assert "--replay=" in output


def test_cli_recovers_completed_events_after_truncated_tail(tmp_path, capsys):
    path = tmp_path / "events.jsonl"
    event = {
        "nodeid": "test_demo.py::test_writer",
        "net_changes": [{"field": "env:MODE", "allowed": False}],
    }
    path.write_text(json.dumps(event) + '\n{"nodeid": "partial')
    assert main(["--events", str(path)]) == 0
    captured = capsys.readouterr()
    assert "test_demo.py::test_writer" in captured.out
    assert "env:MODE" in captured.out
    assert "incomplete final event" in captured.err


def test_cli_rejects_malformed_interior_event(tmp_path, capsys):
    path = tmp_path / "events.jsonl"
    path.write_text('not json\n{"nodeid": "later"}\n')
    assert main(["--events", str(path)]) == 2
    assert "line 1" in capsys.readouterr().err
