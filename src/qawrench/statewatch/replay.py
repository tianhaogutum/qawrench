"""Compose pytest-replay instead of implementing test scheduling or bisection."""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
from collections import Counter
from pathlib import Path

from pytest_replay import ReplayTestInfo

from .reporting import atomic_json

VALUE_OPTIONS = {
    "--statewatch",
    "--statewatch-output",
    "--statewatch-max-replays",
    "--statewatch-replay-timeout",
    "--statewatch-victim",
    "--replay-record-dir",
    "--replay-base-name",
}
FLAGS = {
    "--statewatch-confirm",
    "--statewatch-fail-on-leak",
    "--replay-skip-cleanup",
    "--statewatch-replay-child",
}


def child_arguments(original: list[str]) -> list[str]:
    """Remove only options owned by this adapter and replay recording.

    Existing --replay inputs are rejected for confirmation by the plugin rather
    than guessing where their variable-length values end.
    """
    result = []
    index = 0
    while index < len(original):
        arg = original[index]
        if arg == "--":
            result.extend(original[index:])
            break
        name = arg.split("=", 1)[0]
        if name in VALUE_OPTIONS:
            index += 1 if "=" in arg else 2
        elif arg in FLAGS:
            index += 1
        else:
            result.append(arg)
            index += 1
    return result


def write_replay(path: Path, nodeids: list[str]) -> None:
    # Use the upstream dataclass's own JSON representation, not a new format.
    with path.open("w", encoding="utf-8") as handle:
        for nodeid in dict.fromkeys(nodeids):
            handle.write(json.dumps(ReplayTestInfo(nodeid=nodeid).to_clean_dict()) + "\n")


def command_for(
    original: list[str],
    policy_path: Path,
    output: Path,
    replay_path: Path,
    executable: str | None = None,
) -> list[str]:
    return [
        executable or sys.executable,
        "-m",
        "pytest",
        "-p",
        "replay",
        "-p",
        "statewatch",
        f"--replay={replay_path}",
        f"--statewatch={policy_path}",
        f"--statewatch-output={output}",
        "--statewatch-replay-child",
        *child_arguments(original),
    ]


def shell_command(argv: list[str], cwd: str) -> str:
    return f"cd {shlex.quote(cwd)} && {shlex.join(argv)}"


def stop_child(process) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def run_child(argv: list[str], cwd: str, env: dict[str, str], log: Path, timeout: float) -> dict:
    with log.open("wb") as handle:
        try:
            with subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=os.name == "posix",
            ) as process:
                try:
                    code = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    stop_child(process)
                    return {"status": "timeout", "exit_code": None, "log": str(log)}
                except BaseException:
                    stop_child(process)
                    raise
        except OSError as exc:
            return {"status": "launch_error", "error": type(exc).__name__, "exit_code": None}
    return {"status": "finished", "exit_code": code, "log": str(log)}


def test_outcome(report: dict, nodeid: str) -> str:
    tests = [test for test in report.get("tests", []) if test["nodeid"] == nodeid]
    if len(tests) != 1:
        return "not_run" if not tests else "repeated"
    phases = tests[0]["reports"]
    if any(phase["outcome"] == "failed" for phase in phases):
        return "failed"
    if any(phase["outcome"] == "skipped" for phase in phases):
        return "skipped"
    if {phase["when"] for phase in phases if phase["outcome"] == "passed"} >= {
        "setup",
        "call",
        "teardown",
    }:
        return "passed"
    return "incomplete"


def failure_signatures(records: list[dict], nodeid: str) -> list[dict]:
    return [
        phase["failure_signature"]
        for record in records
        if record["nodeid"] == nodeid
        for phase in record["reports"]
        if phase["outcome"] == "failed" and "failure_signature" in phase
    ]


def confirm(plugin) -> list[dict]:
    """Bounded, serial clean/pair experiments for temporally associated victims."""
    directory = plugin.output / "replays" / plugin.run_id
    directory.mkdir(parents=True, exist_ok=False)
    policy_copy = directory / "policy.json"
    atomic_json(policy_copy, plugin.policy.to_dict())
    budget = plugin.config.getoption("statewatch_max_replays")
    timeout = plugin.config.getoption("statewatch_replay_timeout")
    target_filter = set(plugin.config.getoption("statewatch_victim"))
    results = []
    plugin.confirmations = results
    duplicate_nodeids = {
        nodeid
        for nodeid, count in Counter(r["nodeid"] for r in plugin.records).items()
        if count > 1
    }
    counter = 0

    def experiment(nodeids: list[str]) -> dict:
        nonlocal counter
        number = counter
        counter += 1
        replay_path = directory / f"{number:03d}.jsonl"
        output = directory / f"{number:03d}"
        write_replay(replay_path, nodeids)
        argv = command_for(
            plugin.original_args,
            policy_copy,
            output,
            replay_path,
            executable=plugin.start_executable,
        )
        result = {
            "nodeids": nodeids,
            "replay_file": str(replay_path),
            "argv": argv,
            "command": shell_command(argv, plugin.start_cwd),
        }
        try:
            result.update(
                run_child(
                    argv,
                    plugin.start_cwd,
                    plugin.start_env,
                    directory / f"{number:03d}.log",
                    timeout,
                )
            )
        except KeyboardInterrupt:
            plugin.confirmation_interrupted = True
            result.update(status="interrupted", exit_code=None)
        if result["status"] == "finished":
            try:
                report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                result["victim_outcome"] = "no_report"
            else:
                result["victim_outcome"] = test_outcome(report, nodeids[-1])
                result["observations_complete"] = not report.get("observer_errors")
                result["actual_nodeids"] = [record["nodeid"] for record in report["tests"]]
                result["order_matches"] = result["actual_nodeids"] == nodeids
                result["failure_signatures"] = failure_signatures(report["tests"], nodeids[-1])
        return result

    for record in plugin.records:
        victim = record["nodeid"]
        if target_filter and victim not in target_filter:
            continue
        if not any(phase["outcome"] == "failed" for phase in record["reports"]):
            continue
        candidates = {}
        for inherited in record["inherited"]:
            for key in ("last_writer", "first_writer"):
                writer = inherited[key]
                if writer and writer != victim:
                    candidates.setdefault(writer, []).append(inherited["field"])
        if not candidates:
            continue
        entry = {"victim": victim, "baseline": None, "pairs": [], "status": "budget_exhausted"}
        results.append(entry)
        if victim in duplicate_nodeids or duplicate_nodeids.intersection(candidates):
            entry["status"] = "duplicate_nodeids"
            continue
        if counter >= budget:
            continue
        entry["baseline"] = baseline = experiment([victim])
        if baseline["status"] == "interrupted":
            entry["status"] = "interrupted"
            return results
        if (
            baseline.get("victim_outcome") != "passed"
            or baseline.get("exit_code") != 0
            or not baseline.get("order_matches")
        ):
            entry["status"] = "baseline_not_clean"
            continue
        entry["status"] = "no_pair_reproduced"
        for writer, fields in candidates.items():
            if counter >= budget:
                entry["budget_exhausted"] = True
                if entry["status"] != "pair_reproduced":
                    entry["status"] = "budget_exhausted"
                break
            pair = experiment([writer, victim])
            pair.update({"writer": writer, "fields": sorted(set(fields))})
            pair["execution_status"] = pair["status"]
            original_failures = failure_signatures([record], victim)
            pair["failure_matches"] = (
                bool(original_failures) and pair.get("failure_signatures") == original_failures
            )
            reproduced = (
                pair.get("victim_outcome") == "failed"
                and pair.get("exit_code") == 1
                and pair.get("order_matches")
                and pair["failure_matches"]
            )
            if pair["execution_status"] != "finished":
                pair["status"] = pair["execution_status"]
            elif "order_matches" not in pair:
                pair["status"] = "missing_report"
            elif not pair["order_matches"]:
                pair["status"] = "replay_mismatch"
            elif pair.get("victim_outcome") == "failed" and not pair["failure_matches"]:
                pair["status"] = "different_failure"
            else:
                pair["status"] = "pair_reproduced" if reproduced else "not_reproduced"
            entry["pairs"].append(pair)
            if pair["status"] == "interrupted":
                entry["status"] = "interrupted"
                return results
            if reproduced:
                entry["status"] = "pair_reproduced"
    return results
