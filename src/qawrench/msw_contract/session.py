"""Schema validation in a separate, restartable Python process.

One sequential validation process caches compiled schemas. If a validator stalls
(for example on a catastrophically backtracking ``pattern``) or crashes, it is
killed, the case reports the failure, and a fresh process is started for the
next explicit example.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

import qawrench

from ._process import EOF, TIMEOUT, LineProcess
from .errors import CheckCancelled, CheckError

INIT_TIMEOUT_MS = 10000
LOG_LIMIT = 8192
_BOOT = "import sys; sys.path.insert(0, sys.argv[1]); from qawrench.msw_contract.session import serve; serve()"


def _package_parent() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(qawrench.__file__)))


class ContractSession:
    def __init__(self, schema_path: str, api_root: str, cwd: str):
        self.schema_path = schema_path
        self.api_root = api_root
        self.cwd = cwd
        self.process: LineProcess | None = None
        self.ready = False

    # Lifecycle ---------------------------------------------------------------

    def _spawn(self) -> None:
        # -P: the project directory (cwd) must not shadow qawrench's own imports.
        self.process = LineProcess([sys.executable, "-P", "-c", _BOOT, _package_parent()], cwd=self.cwd,
                                   env=dict(os.environ), log_limit=LOG_LIMIT)
        self.ready = False
        self.process.send({"type": "init", "schemaPath": self.schema_path, "apiRoot": self.api_root})

    def start(self, timeout_ms: float = INIT_TIMEOUT_MS, cancel: threading.Event | None = None) -> None:
        if self.process is None or not self.process.alive():
            self._spawn()
        if self.ready:
            return
        reply = self._await(timeout_ms, cancel, "schema-timeout")
        if not reply.get("ready"):
            error = reply.get("error") or {"code": "validation-process", "message": "Unexpected schema process reply"}
            logs = self.process.logs
            self.close()
            raise CheckError(f"{error['code']}: {error['message']}" + (f"\n{logs}" if logs else ""))
        self.ready = True

    def close(self) -> None:
        process, self.process = self.process, None
        self.ready = False
        if process is not None:
            process.close()

    # Requests ----------------------------------------------------------------

    def _failure(self, code: str, message: str) -> dict:
        return {"error": {"code": code, "message": message}}

    def _await(self, timeout_ms: float, cancel: threading.Event | None, timeout_code: str) -> dict:
        process = self.process
        deadline = time.monotonic() + max(1.0, timeout_ms) / 1000
        while True:
            if cancel is not None and cancel.is_set():
                self.close()
                raise CheckCancelled("Schema process cancelled")
            remaining = deadline - time.monotonic()
            message = process.receive(min(remaining, 0.05) if cancel is not None else remaining)
            if message is TIMEOUT:
                if time.monotonic() >= deadline:
                    process.close()
                    return self._failure(timeout_code, f"Schema processing exceeded its "
                                                       f"{max(0, int(timeout_ms))} ms remaining deadline")
                continue
            if message is EOF or process.overflow:
                process.close()
                if process.overflow:
                    return self._failure("validation-output-limit",
                                         f"Schema process exceeded its {LOG_LIMIT}-byte diagnostic limit")
                return self._failure("validation-process", f"Schema process exited ({process.exit_description()})")
            return message

    def validate(self, example: dict, response: dict, timeout_ms: float,
                 cancel: threading.Event | None = None) -> dict:
        started = time.monotonic()
        try:
            self.start(max(1.0, timeout_ms), cancel)
        except CheckCancelled:
            raise
        except CheckError as exc:
            return {"issues": [{"code": "validation-process", "path": "", "message": str(exc)}]}
        remaining = timeout_ms - (time.monotonic() - started) * 1000
        if remaining <= 0:
            return {"issues": [{"code": "validation-timeout", "path": "",
                                "message": "Case deadline elapsed before schema validation"}]}
        process = self.process
        if process.send({"type": "check", "example": example, "response": response}):
            reply = self._await(remaining, cancel, "validation-timeout")
        else:
            process.close()
            reply = self._failure("validation-process", "Schema process is not accepting requests")
        error = reply.get("error") or {"code": "validation-process", "message": "Unexpected schema process reply"}
        validation = reply.get("validation") or {"issues": [{"path": "", **error}]}
        if process.logs:
            validation["validationDiagnostics"] = process.logs
        if not process.alive():
            # Restart now so the replacement warms up while the next handler runs.
            self.process = None
            self._spawn()
        return validation


def serve() -> None:
    """Child entry point: JSON-line requests on stdin, one reply per line on stdout."""
    import warnings

    warnings.simplefilter("ignore")
    channel = sys.stdout.buffer
    sys.stdout = sys.stderr  # Keep stray output off the reply channel.
    from .contract import check_response, load_contract

    contract = None
    for line in sys.stdin.buffer:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        kind = message.get("type")
        try:
            if kind == "init":
                contract = load_contract(message["schemaPath"], api_root=message["apiRoot"])
                reply = {"ready": True}
            elif kind == "check" and contract is not None:
                reply = {"validation": check_response(contract, message["example"], message["response"])}
            else:
                raise ValueError("Invalid contract-worker command")
        except Exception as exc:  # noqa: BLE001 - every failure becomes a structured reply
            reply = {"error": {"code": "schema-config" if kind == "init" else "validation-error",
                               "message": str(exc) or type(exc).__name__}}
        channel.write(json.dumps(reply).encode("ascii") + b"\n")
        channel.flush()
