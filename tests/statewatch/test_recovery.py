from __future__ import annotations

import os
import subprocess
import sys

import pytest

from qawrench.statewatch.replay import run_child


def test_interrupting_confirmation_preserves_observations(suite):
    conftest = """
def pytest_sessionstart(session):
    if session.config.getoption("statewatch_confirm", default=False):
        import qawrench.statewatch.replay as replay
        def interrupt(*args, **kwargs): raise KeyboardInterrupt
        replay.run_child = interrupt
"""
    source = """
import os
def test_writer(): os.environ["STATEWATCH_TEST_DIRTY"] = "yes"
def test_victim():
    value = os.environ.get("STATEWATCH_TEST_DIRTY")
    assert value is None
"""
    result, report, events, _ = suite(
        source, {"env": ["STATEWATCH_TEST_*"]}, conftest=conftest, args=("--statewatch-confirm",)
    )
    assert result.returncode == 2
    assert report is not None
    assert report["status"] == "confirmation_interrupted"
    assert report["original_exit_code"] == 1
    assert len(report["tests"]) == len(events) == 2
    assert report["confirmations"][0]["status"] == "interrupted"


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group verification")
def test_interrupted_child_is_killed_before_control_returns(tmp_path, monkeypatch):
    original = subprocess.Popen.wait
    process_ids = []

    def interrupted_once(process, timeout=None):
        if not process_ids:
            process_ids.append(process.pid)
            raise KeyboardInterrupt
        return original(process, timeout=timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", interrupted_once)
    with pytest.raises(KeyboardInterrupt):
        run_child(
            [sys.executable, "-c", "import time; time.sleep(1)"],
            str(tmp_path),
            dict(os.environ),
            tmp_path / "child.log",
            5,
        )
    with pytest.raises(ProcessLookupError):
        os.kill(process_ids[0], 0)
