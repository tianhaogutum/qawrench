"""Active observation; imported only after an explicit --statewatch policy."""

from __future__ import annotations

import importlib.metadata
import os
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

import pytest

from . import __version__, replay
from .policy import Policy
from .reporting import ArtifactWriter
from .state import Snapshot, capture, changes, equivalent, visible


class StateWatch:
    def __init__(self, config, policy: Policy, policy_path: Path):
        self.config = config
        self.is_replay_child = config.getoption("statewatch_replay_child")
        self.policy = policy
        self.policy_path = policy_path
        self.output = Path(config.getoption("statewatch_output")).resolve()
        self.start_cwd = os.getcwd()
        self.start_executable = sys.executable
        self.start_env = dict(os.environ)
        self.start_env.pop("PYTEST_CURRENT_TEST", None)
        self.original_args = list(config.invocation_params.args)
        self.run_id = uuid.uuid4().hex
        self.records = []
        self.first_writers = {}
        self.active = {}
        self.baseline: Snapshot | None = None
        self.previous_after: Snapshot | None = None
        self.current = None
        self.observer_errors = []
        self.error_keys = set()
        self.errors_omitted = 0
        self.seen_values = set()
        self.unattributed = []
        self.confirmations = []
        self.confirmation_interrupted = False
        self.started = time.monotonic()
        self.writer = ArtifactWriter(
            self.output,
            {
                "schema_version": 1,
                "run_id": self.run_id,
                "statewatch_version": __version__,
                "pytest_version": pytest.__version__,
                "python_version": sys.version.split()[0],
                "deepdiff_version": importlib.metadata.version("deepdiff"),
                "pytest_replay_version": importlib.metadata.version("pytest-replay"),
                "cwd": self.start_cwd,
                "argv": self.original_args,
                "policy_path": str(policy_path),
                "policy": policy.to_dict(),
                "note": "First observed writers are test boundaries, not exact assignments.",
            },
        )

    def observe(self, phase: str) -> Snapshot:
        snapshot = capture(self.policy)
        nodeid = self.current["nodeid"] if self.current else None
        self.seen_values.update(
            field for field, value in snapshot.values.items() if value["status"] == "value"
        )
        for field, reason in snapshot.errors.items():
            if field in self.active:
                self.active[field] = {"first_writer": None, "last_writer": None}
            key = (nodeid, phase, field, reason)
            if key not in self.error_keys:
                if len(self.error_keys) < 1000:
                    self.error_keys.add(key)
                    self.observer_errors.append(
                        {
                            "nodeid": nodeid,
                            "phase": phase,
                            "field": field,
                            "reason": reason,
                        }
                    )
                else:
                    self.errors_omitted += 1
        return snapshot

    def incoming(self, before: Snapshot) -> list[dict]:
        result = []
        for field, origin in list(self.active.items()):
            if field in before.errors or field in self.baseline.errors:
                continue
            # A known writer was established with a real baseline comparison.
            # If its frozen value is unchanged since the preceding protocol,
            # that result remains valid. Unknown origins still need comparison.
            retained = (
                origin["last_writer"] is not None
                and self.previous_after is not None
                and field not in self.previous_after.errors
                and before.same(self.previous_after, field)
            )
            if before.same(self.baseline, field) or (
                not retained and equivalent(before.get(field), self.baseline.get(field))
            ):
                self.active.pop(field, None)
                continue
            result.append(
                {
                    "field": field,
                    "first_writer": origin["first_writer"],
                    "last_writer": origin["last_writer"],
                }
            )
        return result

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        self.current = {
            "nodeid": item.nodeid,
            "index": len(self.records),
            "reports": [],
            "phase_changes": [],
            "fixtures_started": [],
            "fixtures_finished": [],
        }
        before = self.observe("before_setup")
        if self.baseline is None:
            self.baseline = before
        if self.previous_after is not None:
            for change in changes(self.policy, self.previous_after, before):
                if change["allowed"]:
                    continue
                field = change["field"]
                restored = equivalent(before.get(field), self.baseline.get(field))
                self.unattributed.append(
                    {
                        "before_test": item.nodeid,
                        "field": field,
                        "restored_to_baseline": restored,
                    }
                )
                self.current.setdefault("unattributed_before", []).append(change)
                if field in self.baseline.errors:
                    change["baseline_unavailable"] = True
                    self.active.pop(field, None)
                    continue
                if restored:
                    self.active.pop(field, None)
                else:
                    self.active[field] = {"first_writer": None, "last_writer": None}
        self.current["inherited"] = self.incoming(before)
        self.phase_before = before
        try:
            return (yield)
        finally:
            after = self.observe("after_protocol")
            self.record_phase("protocol_finalizers", after)
            net = changes(self.policy, before, after)
            for change in net:
                if change["allowed"]:
                    continue
                field = change["field"]
                if field in self.baseline.errors:
                    change["baseline_unavailable"] = True
                    self.active.pop(field, None)
                    continue
                if equivalent(after.get(field), self.baseline.get(field)):
                    self.active.pop(field, None)
                    change["restored_to_baseline"] = True
                else:
                    self.first_writers.setdefault(field, item.nodeid)
                    change["first_observed_writer"] = self.first_writers[field]
                    first = self.active.get(field, {}).get("first_writer", item.nodeid)
                    self.active[field] = {"first_writer": first, "last_writer": item.nodeid}
                    change["episode_first_writer"] = first
            self.current["net_changes"] = net
            self.writer.append(self.current)
            self.records.append(
                {
                    "nodeid": item.nodeid,
                    "index": self.current["index"],
                    "reports": self.current["reports"],
                    "changed_fields": [c["field"] for c in net if not c["allowed"]],
                    "leaked_fields": [
                        c["field"]
                        for c in net
                        if not c["allowed"]
                        and not c.get("restored_to_baseline")
                        and not c.get("baseline_unavailable")
                    ],
                    "inherited": self.current["inherited"],
                }
            )
            self.previous_after = after
            self.current = None

    def record_phase(self, phase: str, after: Snapshot) -> None:
        if self.current is None:
            return
        diff = changes(self.policy, self.phase_before, after)
        if diff:
            self.current["phase_changes"].append({"phase": phase, "changes": diff})
        self.phase_before = after

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_setup(self, item):
        try:
            return (yield)
        finally:
            self.record_phase("setup", self.observe("after_setup"))

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_call(self, item):
        try:
            return (yield)
        finally:
            self.record_phase("call", self.observe("after_call"))

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_teardown(self, item, nextitem):
        try:
            return (yield)
        finally:
            self.record_phase("teardown", self.observe("after_teardown"))

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_runtest_makereport(self, item, call):
        report = yield
        if report.failed:
            if call.excinfo is not None:
                exception = call.excinfo.type
                frame = call.excinfo.traceback[-1]
                signature = {
                    "when": call.when,
                    "exception_type": f"{exception.__module__}.{exception.__qualname__}",
                    "file": str(frame.path),
                    "line": frame.lineno + 1,
                }
            else:
                signature = {
                    "when": call.when,
                    "exception_type": "pytest.report_failure",
                    "file": item.location[0],
                    "line": item.location[1],
                }
            report.statewatch_failure_signature = signature
        return report

    def pytest_runtest_logreport(self, report):
        if self.current is not None:
            phase = {"when": report.when, "outcome": report.outcome}
            signature = getattr(report, "statewatch_failure_signature", None)
            if signature is not None:
                phase["failure_signature"] = signature
            self.current["reports"].append(phase)

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        if self.current is not None and request.scope != "function":
            self.current["fixtures_started"].append(
                {
                    "name": request.fixturename,
                    "scope": request.scope,
                }
            )
        return (yield)

    def pytest_fixture_post_finalizer(self, fixturedef, request):
        if self.current is not None and request.scope != "function":
            self.current["fixtures_finished"].append(
                {
                    "name": request.fixturename,
                    "scope": request.scope,
                }
            )

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        if self.records:
            unreadable = {error["field"] for error in self.observer_errors}
            for target in self.policy.variables:
                field = f"var:{target}"
                if field not in self.seen_values and field not in unreadable:
                    self.observer_errors.append(
                        {
                            "nodeid": None,
                            "phase": "sessionfinish",
                            "field": field,
                            "reason": "declared variable never had a readable value; "
                            "verify module and attribute names",
                        }
                    )
        if (
            self.config.getoption("statewatch_confirm")
            and not self.is_replay_child
            and exitstatus in (0, 1)
        ):
            try:
                self.confirmations = replay.confirm(self)
            except KeyboardInterrupt:
                self.confirmation_interrupted = True
        if self.confirmation_interrupted:
            session.exitstatus = pytest.ExitCode.INTERRUPTED
        baseline = (
            {}
            if self.baseline is None
            else {
                field: visible(self.policy, field, value)
                for field, value in self.baseline.values.items()
            }
        )
        leaks = sum(bool(record["leaked_fields"]) for record in self.records)
        net_changes = sum(bool(record["changed_fields"]) for record in self.records)
        unattributed_leaks = any(not change["restored_to_baseline"] for change in self.unattributed)
        if (
            self.config.getoption("statewatch_fail_on_leak")
            and not self.is_replay_child
            and (leaks or self.observer_errors or unattributed_leaks)
        ):
            if session.exitstatus == pytest.ExitCode.OK:
                session.exitstatus = pytest.ExitCode.TESTS_FAILED
        status = "observed"
        if self.confirmation_interrupted:
            status = "confirmation_interrupted"
        elif exitstatus not in (0, 1):
            status = "interrupted"
        elif not self.records:
            status = "not_observed"
        elif self.observer_errors:
            status = "incomplete"
        report = {
            "schema_version": 1,
            "run_id": self.run_id,
            "events_file": str(self.output / "events.jsonl"),
            "manifest_file": str(self.output / "run.json"),
            "status": status,
            "original_exit_code": int(exitstatus),
            "exit_code": int(session.exitstatus),
            "duration_seconds": round(time.monotonic() - self.started, 6),
            "baseline": baseline,
            "tests": self.records,
            "tests_with_net_changes": net_changes,
            "tests_with_leaks": leaks,
            "first_observed_writers": self.first_writers,
            "duplicate_nodeids": sorted(
                nodeid
                for nodeid, count in Counter(r["nodeid"] for r in self.records).items()
                if count > 1
            ),
            "unattributed_changes": self.unattributed,
            "observer_errors": self.observer_errors,
            "observer_errors_omitted": self.errors_omitted,
            "confirmations": self.confirmations,
            "confirmation_interrupted": self.confirmation_interrupted,
            "attribution": "test protocol boundary; fixture setup/cleanup can be responsible",
            "causality": "pair_reproduced means one clean pass then one failing ordered pair; "
            "it does not prove a particular field caused the failure",
        }
        self.writer.finish(report)

    def pytest_terminal_summary(self, terminalreporter):
        terminalreporter.section("statewatch")
        leaks = [record for record in self.records if record["leaked_fields"]]
        terminalreporter.write_line(f"{len(leaks)} test protocol(s) left watched state changed")
        if self.unattributed:
            terminalreporter.write_line(
                f"{len(self.unattributed)} change(s) occurred between observed test protocols"
            )
        for record in leaks[:20]:
            terminalreporter.write_line(
                f"  {record['nodeid']}: {', '.join(record['leaked_fields'])}"
            )
        for confirmation in self.confirmations:
            terminalreporter.write_line(f"  {confirmation['victim']}: {confirmation['status']}")
        if self.observer_errors:
            terminalreporter.write_line(
                f"Observation incomplete: {len(self.observer_errors)} state read error(s)"
            )
        terminalreporter.write_line(f"Report: {self.output / 'report.json'}")

    def pytest_unconfigure(self, config):
        self.writer.close()
