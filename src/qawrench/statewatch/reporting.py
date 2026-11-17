"""Local, crash-readable artifacts. Full events are streamed, never accumulated."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from filelock import FileLock, Timeout


def atomic_json(path: Path, data: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=True, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class ArtifactWriter:
    def __init__(self, directory: Path, manifest: dict):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        lock_path = directory / ".statewatch.lock"
        if lock_path.is_symlink():
            raise OSError(f"statewatch output lock must not be a symlink: {lock_path}")
        self.lock = FileLock(lock_path)
        try:
            self.lock.acquire(timeout=0)
        except Timeout as exc:
            raise OSError(
                f"another statewatch run is using {directory}; choose a different output"
            ) from exc
        self.events = None
        try:
            (directory / "report.json").unlink(missing_ok=True)
            atomic_json(directory / "run.json", manifest)
            stream = tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=directory, prefix=".events.", delete=False
            )
            temporary = Path(stream.name)
            try:
                os.replace(temporary, directory / "events.jsonl")
                self.events = stream
            except BaseException:
                stream.close()
                temporary.unlink(missing_ok=True)
                raise
        except BaseException:
            self.close()
            raise

    def append(self, event: dict) -> None:
        self.events.write(json.dumps(event, ensure_ascii=True, allow_nan=False) + "\n")
        self.events.flush()

    def finish(self, report: dict) -> None:
        atomic_json(self.directory / "report.json", report)

    def close(self) -> None:
        if self.events is not None:
            self.events.close()
        self.lock.release()
