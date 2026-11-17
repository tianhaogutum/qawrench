"""A child process that exchanges one JSON document per line over stdin/stdout."""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading

from ._js import utf8_prefix

EOF = object()
TIMEOUT = object()


class LineProcess:
    """Reads JSON lines from stdout on a thread; bounds and keeps stderr as diagnostics."""

    def __init__(self, argv: list[str], *, cwd: str, env: dict[str, str], log_limit: int | None = None,
                 new_session: bool = False):
        self.log_limit = log_limit
        self.logs = ""
        self.log_bytes = 0
        self.overflow = False
        self._new_session = new_session and os.name == "posix"
        self._messages: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, start_new_session=self._new_session)
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        try:
            for line in self.proc.stdout:
                try:
                    self._messages.put(json.loads(line))
                except ValueError:
                    self._messages.put({"type": "protocol-error", "line": line[:200].decode("utf-8", "replace")})
        except (OSError, ValueError):
            pass
        finally:
            self._messages.put(EOF)

    def _read_stderr(self) -> None:
        limit = 8192 if self.log_limit is None else self.log_limit
        try:
            while chunk := self.proc.stderr.read1(65536):
                with self._lock:
                    self.logs += utf8_prefix(chunk, max(0, limit - self.log_bytes))
                    self.log_bytes += len(chunk)
                    exceeded = self.log_limit is not None and self.log_bytes > self.log_limit and not self.overflow
                    if exceeded:
                        self.overflow = True
                if exceeded:
                    self.kill()
        except (OSError, ValueError):
            pass

    def alive(self) -> bool:
        return self.proc.poll() is None

    def send(self, message: dict) -> bool:
        try:
            self.proc.stdin.write(json.dumps(message).encode("ascii") + b"\n")
            self.proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def receive(self, timeout: float):
        try:
            return self._messages.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return TIMEOUT

    def kill(self) -> None:
        if self.proc.poll() is not None:
            return
        try:
            if self._new_session:
                os.killpg(self.proc.pid, signal.SIGKILL)  # the runner and its handler workers
            else:
                self.proc.kill()
        except (ProcessLookupError, PermissionError):
            pass

    def exit_description(self) -> str:
        code = self.proc.poll()
        if code is None:
            return "code null, signal null"
        if code < 0:
            try:
                return f"code null, signal {signal.Signals(-code).name}"
            except ValueError:
                return f"code null, signal {-code}"
        return f"code {code}, signal null"

    def close(self) -> None:
        self.kill()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
