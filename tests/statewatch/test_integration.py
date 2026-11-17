from __future__ import annotations

import json
from pathlib import Path

from .conftest import FIXTURES

POLICY = {"env": ["STATEWATCH_TEST_*"], "cwd": True, "variables": ["test_subject:STATE"]}


def test_upstream_demo_and_actual_replay(suite):
    upstream = FIXTURES / "leak_finder_demo.py"
    source = upstream.read_text()
    # Only the file/module name changes when pytest collects this upstream file.
    result, report, events, root = suite(
        source,
        {"variables": ["test_subject:leak_state"]},
        args=("--statewatch-confirm",),
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert report["first_observed_writers"] == {
        "var:test_subject:leak_state": "test_subject.py::test3"
    }
    writer = events[2]
    assert writer["net_changes"][0]["diff"]["iterable_item_added"] == {"root['value'][0]": "leak"}
    confirmation = report["confirmations"][0]
    assert confirmation["baseline"]["victim_outcome"] == "passed"
    assert confirmation["status"] == "pair_reproduced"
    assert confirmation["pairs"][0]["nodeids"] == [
        "test_subject.py::test3",
        "test_subject.py::test5",
    ]
    pair_input = Path(confirmation["pairs"][0]["replay_file"])
    assert [json.loads(line)["nodeid"] for line in pair_input.read_text().splitlines()] == (
        confirmation["pairs"][0]["nodeids"]
    )


def test_monkeypatch_clean_and_direct_changes_attributed(suite):
    source = """
import os
STATE = {"headers": []}
def test_clean(monkeypatch, tmp_path):
    monkeypatch.setenv("STATEWATCH_TEST_MODE", "temporary")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(STATE, "headers", ["temporary"])
def test_polluter(tmp_path):
    os.environ["STATEWATCH_TEST_MODE"] = "debug"
    os.chdir(tmp_path)
    STATE["headers"].append("debug")
def test_victim():
    mode = os.environ.get("STATEWATCH_TEST_MODE")
    assert mode is None
"""
    result, report, events, _ = suite(source, POLICY, args=("--statewatch-confirm",))
    assert result.returncode == 1, result.stdout + result.stderr
    assert not events[0]["net_changes"]
    assert {phase["phase"] for phase in events[0]["phase_changes"]} == {"call", "teardown"}
    assert report["tests"][1]["leaked_fields"] == [
        "cwd",
        "env:STATEWATCH_TEST_MODE",
        "var:test_subject:STATE",
    ]
    assert len(events[2]["inherited"]) == 3
    assert report["confirmations"][0]["status"] == "pair_reproduced"


def test_teardown_leak_and_fixture_context(suite):
    source = """
import os
import pytest
@pytest.fixture
def dirty_finalizer():
    yield
    os.environ["STATEWATCH_TEST_FINALIZER"] = "leftover"
def test_fixture(dirty_finalizer): pass
def test_victim():
    value = os.environ.get("STATEWATCH_TEST_FINALIZER")
    assert value is None
"""
    result, report, events, _ = suite(source, {"env": ["STATEWATCH_TEST_*"]})
    assert result.returncode == 1
    assert events[0]["phase_changes"][0]["phase"] == "teardown"
    assert report["first_observed_writers"]["env:STATEWATCH_TEST_FINALIZER"] == (
        "test_subject.py::test_fixture"
    )


def test_setup_error_still_records_leak(suite):
    source = """
import os
import pytest
@pytest.fixture
def broken():
    os.environ["STATEWATCH_TEST_BROKEN"] = "yes"
    raise RuntimeError("setup failed")
def test_setup(broken): pass
def test_victim():
    value = os.environ.get("STATEWATCH_TEST_BROKEN")
    assert value is None
"""
    result, report, events, _ = suite(source, {"env": ["STATEWATCH_TEST_*"]})
    assert result.returncode == 1
    assert events[0]["reports"][0]["when"] == "setup"
    assert events[0]["reports"][0]["outcome"] == "failed"
    assert events[0]["reports"][0]["failure_signature"]["exception_type"] == "builtins.RuntimeError"
    assert events[1]["inherited"][0]["first_writer"] == "test_subject.py::test_setup"
    assert report["observer_errors"] == []


def test_redaction_applies_to_every_state_artifact(suite):
    source = """
import os
STATE = "private-declared-value"
def test_changes():
    os.environ["STATEWATCH_TEST_API_TOKEN"] = "private-env-value"
    global STATE
    STATE = "another-private-value"
"""
    policy = {
        "env": ["STATEWATCH_TEST_*"],
        "variables": ["test_subject:STATE"],
        "redact": ["var:*"],
    }
    result, report, events, root = suite(source, policy)
    assert result.returncode == 0
    assert len(events[0]["net_changes"]) == 2
    assert all(c["diff"]["redacted"] for c in events[0]["net_changes"])
    for file in (root / "artifacts").glob("*.json*"):
        content = file.read_text()
        assert "private-env-value" not in content
        assert "private-declared-value" not in content
        assert "another-private-value" not in content
    assert report["tests_with_net_changes"] == 1


def test_allow_list_is_visible_but_not_a_leak(suite):
    source = """
import os
def test_changes(): os.environ["STATEWATCH_TEST_OK"] = "yes"
def test_victim(): assert False
"""
    result, report, events, _ = suite(
        source,
        {"env": ["STATEWATCH_TEST_*"], "allow": ["env:STATEWATCH_TEST_OK"]},
        args=("--statewatch-confirm",),
    )
    assert result.returncode == 1
    assert events[0]["net_changes"][0]["allowed"]
    assert report["tests_with_net_changes"] == 0
    assert report["first_observed_writers"] == {}
    assert report["confirmations"] == []


def test_unsupported_state_is_incomplete_and_can_fail_ci(suite):
    result, report, events, _ = suite(
        "STATE = object()\ndef test_ok(): pass\n",
        {"variables": ["test_subject:STATE"]},
        args=("--statewatch-fail-on-leak",),
    )
    assert result.returncode == 1
    assert report["status"] == "incomplete"
    assert report["original_exit_code"] == 0
    assert report["observer_errors"][0]["reason"].startswith("unsupported object")
    assert not events[0]["net_changes"]


def test_no_implicit_import_or_dynamic_property_execution(suite):
    source = """
class Settings:
    @property
    def dangerous(self):
        raise RuntimeError("should never run")
STATE = Settings()
def test_ok(): pass
"""
    result, report, _, root = suite(
        source,
        {"variables": ["never_imported:X", "test_subject:STATE.dangerous"]},
        files={"never_imported.py": 'raise RuntimeError("implicit import")\n'},
    )
    assert result.returncode == 0
    assert report["baseline"]["var:never_imported:X"] == {"status": "unloaded"}
    assert report["observer_errors"][0]["reason"].startswith("unsupported property")
    assert "should never run" not in (root / "artifacts/events.jsonl").read_text()


def test_permanently_failing_victim_does_not_confirm_causality(suite):
    source = """
import os
def test_polluter(): os.environ["STATEWATCH_TEST_DIRTY"] = "yes"
def test_victim(): assert False
"""
    result, report, _, _ = suite(
        source, {"env": ["STATEWATCH_TEST_*"]}, args=("--statewatch-confirm",)
    )
    assert result.returncode == 1
    confirmation = report["confirmations"][0]
    assert confirmation["status"] == "baseline_not_clean"
    assert confirmation["baseline"]["victim_outcome"] == "failed"
    assert confirmation["pairs"] == []


def test_skipped_baseline_is_not_a_clean_pass(suite):
    source = """
import os
import pytest
def test_polluter(): os.environ["STATEWATCH_TEST_DIRTY"] = "yes"
def test_victim():
    if "STATEWATCH_TEST_DIRTY" not in os.environ:
        pytest.skip("requires dirty state")
    assert False
"""
    result, report, _, _ = suite(
        source, {"env": ["STATEWATCH_TEST_*"]}, args=("--statewatch-confirm",)
    )
    assert result.returncode == 1
    assert report["confirmations"][0]["baseline"]["victim_outcome"] == "skipped"
    assert report["confirmations"][0]["status"] == "baseline_not_clean"


def test_confirmation_timeout_is_bounded(suite):
    source = """
import os
import time
def test_polluter(): os.environ["STATEWATCH_TEST_DIRTY"] = "yes"
def test_victim(request):
    if request.config.getoption("replay_files", default=[]): time.sleep(10)
    value = os.environ.get("STATEWATCH_TEST_DIRTY")
    assert value is None
"""
    result, report, _, _ = suite(
        source,
        {"env": ["STATEWATCH_TEST_*"]},
        args=(
            "--statewatch-confirm",
            "--statewatch-replay-timeout=0.3",
        ),
    )
    assert result.returncode == 1
    assert report["confirmations"][0]["baseline"]["status"] == "timeout"


def test_collection_failure_has_a_final_report(suite):
    result, report, events, _ = suite('raise RuntimeError("collection failed")\n', {"cwd": True})
    assert result.returncode == 2
    assert report["original_exit_code"] == 2
    assert report["tests"] == []
    assert events == []


def test_plugin_is_dormant_without_policy(suite):
    result, report, events, root = suite("def test_ok(): pass\n")
    assert result.returncode == 0
    assert report is None
    assert not events
    assert not (root / ".statewatch").exists()


def test_entrypoint_autoload_and_explicit_plugin_are_compatible(suite):
    result, report, _, _ = suite("def test_ok(): pass\n", {"cwd": True}, autoload=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert report["tests_with_net_changes"] == 0


def test_restoring_dirty_state_is_a_change_but_not_a_new_leak(suite):
    source = """
STATE = []
def test_first_writer(): STATE.append("first")
def test_restore(): STATE.clear()
def test_new_episode(): STATE.append("second")
def test_victim(): assert STATE == []
"""
    result, report, events, _ = suite(source, {"variables": ["test_subject:STATE"]})
    assert result.returncode == 1
    assert report["tests"][1]["leaked_fields"] == []
    assert events[1]["net_changes"][0]["restored_to_baseline"]
    assert events[3]["inherited"][0]["first_writer"] == "test_subject.py::test_new_episode"
    assert report["first_observed_writers"]["var:test_subject:STATE"] == (
        "test_subject.py::test_first_writer"
    )
    assert report["tests_with_net_changes"] == 3
    assert report["tests_with_leaks"] == 2


def test_scoped_fixture_lifetime_is_recorded(suite):
    source = """
import os
import pytest
@pytest.fixture(scope="module", autouse=True)
def scoped_config():
    os.environ["STATEWATCH_TEST_SCOPED"] = "shared"
    yield
    del os.environ["STATEWATCH_TEST_SCOPED"]
def test_first(): pass
def test_last(): pass
"""
    result, report, events, _ = suite(source, {"env": ["STATEWATCH_TEST_*"]})
    assert result.returncode == 0
    assert events[0]["fixtures_started"] == [{"name": "scoped_config", "scope": "module"}]
    assert events[1]["fixtures_finished"] == [{"name": "scoped_config", "scope": "module"}]
    assert report["tests"][1]["leaked_fields"] == []
    assert report["tests_with_leaks"] == 1


def test_outside_protocol_changes_are_not_assigned_to_a_test(suite):
    conftest = """
import os
import pytest
class OutsideProtocol:
    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        result = yield
        if item.name == "test_first":
            os.environ["STATEWATCH_TEST_OUTSIDE"] = "outside"
        return result
def pytest_sessionstart(session):
    session.config.pluginmanager.register(OutsideProtocol())
"""
    source = """
import os
def test_first(): pass
def test_victim():
    value = os.environ.get("STATEWATCH_TEST_OUTSIDE")
    assert value is None
"""
    result, report, events, _ = suite(
        source,
        {"env": ["STATEWATCH_TEST_*"]},
        conftest=conftest,
        args=("--statewatch-confirm",),
    )
    assert result.returncode == 1
    assert events[0]["net_changes"] == []
    assert len(report["unattributed_changes"]) == 1
    assert events[1]["inherited"][0]["first_writer"] is None
    assert report["confirmations"] == []


def test_replay_verifies_actual_pair_order(suite):
    source = """
import os
def test_writer(): os.environ["STATEWATCH_TEST_DIRTY"] = "yes"
def test_victim(request):
    if request.config.getoption("replay_files", default=[]) and len(request.session.items) > 1:
        raise AssertionError("different collection-order-dependent failure")
    value = os.environ.get("STATEWATCH_TEST_DIRTY")
    assert value is None
"""
    conftest = """
import pytest
@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    if config.getoption("replay_files", default=[]): items.reverse()
"""
    result, report, _, _ = suite(
        source,
        {"env": ["STATEWATCH_TEST_*"]},
        conftest=conftest,
        args=("--statewatch-confirm",),
    )
    assert result.returncode == 1
    confirmation = report["confirmations"][0]
    assert confirmation["status"] != "pair_reproduced"
    pair = confirmation["pairs"][0]
    assert pair["status"] == "replay_mismatch"
    assert pair["actual_nodeids"] == [
        "test_subject.py::test_victim",
        "test_subject.py::test_writer",
    ]


def test_replay_uses_original_interpreter_even_if_test_changes_sys_executable(suite):
    source = """
import os
import sys
def test_writer():
    sys.executable = "/no/such/python"
    os.environ["STATEWATCH_TEST_DIRTY"] = "yes"
def test_victim():
    value = os.environ.get("STATEWATCH_TEST_DIRTY")
    assert value is None
"""
    result, report, _, _ = suite(
        source,
        {"env": ["STATEWATCH_TEST_*"], "variables": ["sys:executable"]},
        args=("--statewatch-confirm",),
    )
    assert result.returncode == 1
    assert report["confirmations"][0]["status"] == "pair_reproduced"


def test_unloaded_or_misspelled_declared_variables_are_incomplete(suite):
    result, report, _, _ = suite(
        "def test_ok(): pass\n",
        {"variables": ["not_loaded:STATE", "test_subject:TYPO"]},
        args=("--statewatch-fail-on-leak",),
    )
    assert result.returncode == 1
    assert report["status"] == "incomplete"
    assert {error["field"] for error in report["observer_errors"]} == {
        "var:not_loaded:STATE",
        "var:test_subject:TYPO",
    }


def test_no_test_protocols_is_not_reported_as_observed(suite):
    result, report, _, _ = suite("def test_ok(): pass\n", {"cwd": True}, args=("--collect-only",))
    assert result.returncode == 0
    assert report["status"] == "not_observed"


def test_confirmation_with_autoload_enabled(suite):
    source = """
import os
def test_writer(): os.environ["STATEWATCH_TEST_DIRTY"] = "yes"
def test_victim():
    value = os.environ.get("STATEWATCH_TEST_DIRTY")
    assert value is None
"""
    result, report, _, _ = suite(
        source,
        {"env": ["STATEWATCH_TEST_*"]},
        autoload=True,
        args=("--statewatch-confirm",),
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert report["confirmations"][0]["status"] == "pair_reproduced"


def test_pair_with_a_different_failure_is_not_claimed_as_reproduced(suite):
    source = """
import os
def test_writer(): os.environ["STATEWATCH_TEST_DIRTY"] = "yes"
def test_victim(request):
    if request.config.getoption("replay_files", default=[]) and len(request.session.items) > 1:
        raise ValueError("an unrelated replay failure")
    value = os.environ.get("STATEWATCH_TEST_DIRTY")
    assert value is None
"""
    result, report, _, _ = suite(
        source,
        {"env": ["STATEWATCH_TEST_*"]},
        args=("--statewatch-confirm",),
    )
    assert result.returncode == 1
    confirmation = report["confirmations"][0]
    assert confirmation["status"] != "pair_reproduced"
    assert confirmation["pairs"][0]["status"] == "different_failure"


def test_same_exception_type_at_different_location_is_not_same_failure(suite):
    source = """
import os
def test_writer(): os.environ["STATEWATCH_TEST_DIRTY"] = "yes"
def test_victim(request):
    if request.config.getoption("replay_files", default=[]) and len(request.session.items) > 1:
        assert False, "another assertion"
    value = os.environ.get("STATEWATCH_TEST_DIRTY")
    assert value is None
"""
    result, report, _, _ = suite(
        source,
        {"env": ["STATEWATCH_TEST_*"]},
        args=("--statewatch-confirm",),
    )
    assert result.returncode == 1
    assert report["confirmations"][0]["pairs"][0]["status"] == "different_failure"


def test_observation_gap_does_not_preserve_stale_writer_attribution(suite):
    source = """
STATE = None
def test_first_writer():
    global STATE
    STATE = 1
def test_unreadable():
    global STATE
    STATE = object()
def test_recovery():
    global STATE
    STATE = 2
def test_victim(): assert STATE == 0
"""
    result, report, events, _ = suite(
        source,
        {"variables": ["test_subject:STATE"]},
        args=("--statewatch-confirm",),
    )
    assert result.returncode == 1
    assert report["status"] == "incomplete"
    assert events[3]["inherited"] == [
        {"field": "var:test_subject:STATE", "first_writer": None, "last_writer": None}
    ]
    assert report["confirmations"] == []


def test_deleted_working_directory_is_reported_without_crashing(suite):
    source = """
import os
from pathlib import Path
ORIGINAL = str(Path.cwd())
def test_delete_cwd(tmp_path):
    target = tmp_path / "gone"
    target.mkdir()
    os.chdir(target)
    target.rmdir()
def test_restore(): os.chdir(ORIGINAL)
"""
    result, report, _, _ = suite(
        source,
        {"cwd": True},
        args=("--statewatch-fail-on-leak",),
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert report["status"] == "incomplete"
    assert any(error["reason"] == "FileNotFoundError" for error in report["observer_errors"])


def test_unknown_initial_value_cannot_establish_baseline_leaks(suite):
    source = """
STATE = object()
def test_becomes_readable():
    global STATE
    STATE = 1
def test_observed_mutation():
    global STATE
    STATE = 2
def test_victim(): assert STATE == 0
"""
    result, report, events, _ = suite(source, {"variables": ["test_subject:STATE"]})
    assert result.returncode == 1
    assert report["status"] == "incomplete"
    assert events[1]["net_changes"][0]["baseline_unavailable"]
    assert events[2]["inherited"] == []


def test_stale_report_is_removed_when_a_new_session_starts(suite):
    source = """
import os
def test_abrupt_exit(): os._exit(7)
"""
    stale = json.dumps({"schema_version": 1, "stale": True})
    result, report, events, root = suite(
        source,
        {"cwd": True},
        files={"artifacts/report.json": stale},
    )
    assert result.returncode == 7
    assert report is None
    assert (root / "artifacts/run.json").exists()
    assert events == []


def test_locked_artifact_directory_is_rejected(suite, tmp_path):
    from filelock import FileLock

    directory = tmp_path / "artifacts"
    directory.mkdir()
    with FileLock(directory / ".statewatch.lock"):
        result, report, events, _ = suite("def test_ok(): pass\n", {"cwd": True})
    assert result.returncode == 4, result.stdout + result.stderr
    assert "another statewatch run" in result.stderr
    assert report is None
    assert events == []


def test_abrupt_exit_keeps_completed_events_and_releases_the_lock(suite):
    source = """
import os
def test_complete(): pass
def test_abrupt_exit(): os._exit(7)
"""
    result, report, events, _ = suite(source, {"cwd": True})
    assert result.returncode == 7
    assert report is None
    assert [event["nodeid"] for event in events] == ["test_subject.py::test_complete"]
    result, report, _, _ = suite("def test_recovered(): pass\n", {"cwd": True})
    assert result.returncode == 0, result.stdout + result.stderr
    assert [record["nodeid"] for record in report["tests"]] == ["test_subject.py::test_recovered"]


def test_dormant_plugin_does_not_import_observation_dependencies(suite):
    source = """
import sys
def test_dormant():
    assert "deepdiff" not in sys.modules
    assert "filelock" not in sys.modules
    assert "pytest_replay" not in sys.modules
"""
    result, _, _, _ = suite(source)
    assert result.returncode == 0, result.stdout + result.stderr


def test_total_snapshot_budget_is_reported_as_incomplete(suite):
    source = 'ONE = "a" * 700\nTWO = "b" * 700\ndef test_ok(): pass\n'
    result, report, _, _ = suite(
        source,
        {
            "variables": ["test_subject:ONE", "test_subject:TWO"],
            "max_value_bytes": 1024,
            "max_snapshot_bytes": 1024,
        },
        args=("--statewatch-fail-on-leak",),
    )
    assert result.returncode == 1
    assert report["status"] == "incomplete"
    assert any("snapshot" in error["reason"] for error in report["observer_errors"])


def test_type_changes_are_not_lost_by_snapshot_fast_path(suite):
    source = """
STATE = {"nested": [1, True, None]}
def test_float_change(): STATE["nested"][0] = 1.0
def test_bool_change(): STATE["nested"][1] = 1
def test_restore(): STATE["nested"] = [1, True, None]
"""
    result, report, events, _ = suite(source, {"variables": ["test_subject:STATE"]})
    assert result.returncode == 0
    assert all(event["net_changes"] for event in events)
    assert report["tests_with_leaks"] == 2
    assert report["tests"][2]["leaked_fields"] == []


def test_dictionary_order_change_is_not_a_state_change(suite):
    source = """
STATE = {"one": 1, "two": 2}
def test_reordered():
    global STATE
    STATE = {"two": 2, "one": 1}
"""
    result, report, events, _ = suite(source, {"variables": ["test_subject:STATE"]})
    assert result.returncode == 0
    assert events[0]["net_changes"] == []
    assert report["tests_with_leaks"] == 0
