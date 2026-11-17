"""Execute explicit request cases with the project's real MSW handlers through Node.js.

``node/runner.mjs`` is shipped inside this package. It is started once per check
as a small supervisor that resolves ``msw`` from the handlers file (then the
working directory), and forks one fresh, bounded Node process per case under
Node's permission model, exactly like the original ``worker.mjs``/``runner.mjs``
pair. The supervisor prints one JSON result per case on stdout.
"""
from __future__ import annotations

import functools
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

from ._process import EOF, TIMEOUT, LineProcess
from .errors import CheckCancelled, CheckError, RuntimeUnavailable

RUNNER_SCRIPT = Path(__file__).resolve().parent / "node" / "runner.mjs"
MIN_NODE_MAJOR = 24
START_TIMEOUT_SECONDS = 30.0
# The runner enforces each case deadline itself; this only guards against a stuck runner.
GRACE_SECONDS = 5.0
DEFAULT_TIMEOUT_MS = 3000
DEFAULT_MAX_BODY_BYTES = 1024 * 1024


@functools.lru_cache(maxsize=8)
def node_version(node: str) -> str:
    """The ``vX.Y.Z`` version of ``node``; raises ``RuntimeUnavailable`` below Node.js 24."""
    try:
        result = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeUnavailable(f"cannot run Node.js at {node}: {exc}") from None
    version = result.stdout.strip()
    match = re.fullmatch(r"v(\d+)\.\d+\.\d+.*", version)
    if result.returncode != 0 or not match:
        raise RuntimeUnavailable(f"cannot determine the Node.js version of {node}")
    if int(match.group(1)) < MIN_NODE_MAJOR:
        raise RuntimeUnavailable(f"Node.js {MIN_NODE_MAJOR} or newer is required to execute MSW handlers; "
                                 f"found {version} at {node}")
    return version


def find_node(node: str | None = None) -> str:
    """Locate a usable ``node`` executable (``shutil.which("node")`` unless given)."""
    path = node or shutil.which("node")
    if not path:
        raise RuntimeUnavailable(f"Node.js was not found on PATH; install Node.js {MIN_NODE_MAJOR} or newer "
                                 "to execute MSW handlers")
    node_version(path)
    return path


class NodeRunner:
    """One runner process per check; one forked worker process per case."""

    def __init__(self, handlers_path: str, *, cwd: str, node: str | None = None):
        self.handlers_path = handlers_path
        self.cwd = cwd
        self.node = node
        self.process: LineProcess | None = None
        self.info: dict | None = None

    def start(self, cancel: threading.Event | None = None) -> dict:
        node = find_node(self.node)
        self.close()
        self.process = LineProcess([node, str(RUNNER_SCRIPT)], cwd=self.cwd, env=dict(os.environ),
                                   new_session=True)
        self.process.send({"type": "start", "handlersPath": self.handlers_path})
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while True:
            if cancel is not None and cancel.is_set():
                self.close()
                raise CheckCancelled("Handler execution cancelled")
            message = self.process.receive(min(deadline - time.monotonic(), 0.05))
            if message is TIMEOUT:
                if time.monotonic() >= deadline:
                    self.close()
                    raise CheckError(f"the Node runner did not start within {START_TIMEOUT_SECONDS:g} s")
                continue
            if message is EOF:
                logs = self.process.logs.strip()
                self.close()
                detail = logs.splitlines()[0] if logs else "no output"
                raise RuntimeUnavailable(f"the Node runner exited before it was ready: {detail}")
            if message.get("type") == "ready":
                self.info = message
                return message
            if message.get("type") == "fatal":
                self.close()
                raise RuntimeUnavailable(message.get("message") or "the Node runner could not start")

    def run(self, example: dict, *, export_name: str = "handlers", timeout_ms: int = DEFAULT_TIMEOUT_MS,
            max_body_bytes: int = DEFAULT_MAX_BODY_BYTES, base_url: str | None = None,
            cancel: threading.Event | None = None) -> dict:
        """Execute one normalized case; returns ``{"response": ...}`` or ``{"error": ...}``."""
        if self.process is None or not self.process.alive():
            self.start(cancel)
        sent = self.process.send({"type": "case", "exportName": export_name, "example": example,
                                  "maxBodyBytes": max_body_bytes, "baseUrl": base_url, "timeoutMs": timeout_ms})
        if not sent:
            self.close()
            return {"error": {"code": "process-error", "message": "The Node runner is not accepting cases"}}
        deadline = time.monotonic() + timeout_ms / 1000 + GRACE_SECONDS
        cancelling = False
        while True:
            if cancel is not None and cancel.is_set() and not cancelling:
                # The runner kills the worker, waits for its exit and still reports its output.
                cancelling = True
                self.process.send({"type": "cancel"})
                deadline = min(deadline, time.monotonic() + GRACE_SECONDS)
            message = self.process.receive(min(deadline - time.monotonic(), 0.05))
            if message is TIMEOUT:
                if time.monotonic() >= deadline:
                    self.close()
                    if cancelling:
                        return {"error": {"code": "cancelled", "message": "Handler execution cancelled"}}
                    return {"error": {"code": "timeout", "message": f"Handler process exceeded {timeout_ms} ms "
                                                                    "(including import and body capture)"}}
                continue
            if message is EOF:
                logs = self.process.logs
                self.close()
                result = {"error": {"code": "process-exit",
                                    "message": "The Node runner exited before returning a response"}}
                return {**result, "diagnostics": logs} if logs else result
            if message.get("type") == "result":
                return message["result"]

    def close(self) -> None:
        process, self.process = self.process, None
        if process is not None:
            process.close()


def run_handler(handlers_path: str | os.PathLike[str], example: dict, *, export_name: str = "handlers",
                timeout_ms: int = DEFAULT_TIMEOUT_MS, max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
                cwd: str | os.PathLike[str] | None = None, base_url: str | None = None,
                cancel: threading.Event | None = None, node: str | None = None) -> dict:
    """Run one normalized example (``id``, ``method``, ``url``, ``headers``, optional ``body``)."""
    cwd = os.path.abspath(os.fspath(cwd) if cwd is not None else os.getcwd())
    runner = NodeRunner(os.path.abspath(os.path.join(cwd, os.fspath(handlers_path))), cwd=cwd, node=node)
    try:
        return runner.run(example, export_name=export_name, timeout_ms=timeout_ms, max_body_bytes=max_body_bytes,
                          base_url=base_url, cancel=cancel)
    finally:
        runner.close()
