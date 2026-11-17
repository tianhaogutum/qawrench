"""Lightweight opt-in pytest entry point.

Registered through the ``pytest11`` entry point ``statewatch`` whenever qawrench
is installed, so this module imports only the standard library, pytest and the
stdlib-only policy loader. The observer and its dependencies (DeepDiff,
filelock, pytest-replay) are imported in ``pytest_configure``, and only after
``--statewatch POLICY.json`` was given.
"""

import argparse
from pathlib import Path

import pytest

from .policy import Policy, PolicyError

# Top-level import names of the observation dependencies (the `statewatch` extra).
OPTIONAL_DEPENDENCIES = ("deepdiff", "filelock", "pytest_replay")
EXTRA = "qawrench[statewatch]"


def missing_dependency_hint(dependency: str) -> str:
    return (f"qawrench statewatch needs {dependency}: pipx install '{EXTRA}' "
            f"(or pip install '{EXTRA}')")


def pytest_addoption(parser):
    group = parser.getgroup("statewatch", "observe persistent test state")
    group.addoption("--statewatch", metavar="POLICY.json", help="Observe selected state")
    group.addoption(
        "--statewatch-replay-child", action="store_true", default=False, help=argparse.SUPPRESS
    )
    group.addoption("--statewatch-output", default=".statewatch", metavar="DIR")
    group.addoption("--statewatch-fail-on-leak", action="store_true", default=False)
    group.addoption(
        "--statewatch-confirm",
        action="store_true",
        default=False,
        help="Replay associated failing victims alone and after observed writers",
    )
    group.addoption(
        "--statewatch-victim",
        action="append",
        default=[],
        metavar="NODEID",
        help="Restrict confirmation to this exact victim (repeatable)",
    )
    group.addoption("--statewatch-max-replays", type=int, default=12, metavar="N")
    group.addoption("--statewatch-replay-timeout", type=float, default=30.0, metavar="SECONDS")


def _load_observer():
    try:
        from .observer import StateWatch
    except ModuleNotFoundError as exc:
        root = (exc.name or "").partition(".")[0]
        if root in OPTIONAL_DEPENDENCIES:
            raise pytest.UsageError(missing_dependency_hint(root)) from None
        raise
    return StateWatch


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    path = config.getoption("statewatch")
    if path is None:
        return
    if config.getoption("lanes", default=0):
        raise pytest.UsageError("statewatch observes serial pytest; run without --lanes")
    if config.getoption("statewatch_replay_child"):
        config.option.statewatch_confirm = False
        config.option.statewatch_fail_on_leak = False
    if config.getoption("numprocesses", default=0) not in (None, 0, "0"):
        raise pytest.UsageError("statewatch observes one process; use -n 0 to disable xdist")
    if hasattr(config, "workerinput"):
        raise pytest.UsageError("statewatch cannot observe an xdist worker")
    if config.getoption("statewatch_max_replays") < 1:
        raise pytest.UsageError("--statewatch-max-replays must be at least 1")
    timeout = config.getoption("statewatch_replay_timeout")
    if not 0 < timeout <= 3600:
        raise pytest.UsageError("--statewatch-replay-timeout must be in (0, 3600]")
    if config.getoption("statewatch_confirm") and config.getoption("replay_files", default=[]):
        raise pytest.UsageError(
            "confirmation cannot be nested inside --replay; observe that replay first, "
            "then confirm the selected tests without --replay"
        )
    StateWatch = _load_observer()

    policy_path = Path(path).resolve()
    try:
        policy = Policy.load(policy_path)
        plugin = StateWatch(config, policy, policy_path)
    except (PolicyError, OSError) as exc:
        raise pytest.UsageError(f"statewatch: {exc}") from exc
    config.pluginmanager.register(plugin, "statewatch-observer")
