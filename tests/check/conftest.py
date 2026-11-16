"""Shared helpers for the ``qawrench check`` tests. Every fixture is synthetic."""
import copy
import json
from pathlib import Path
import shutil
import sys
import zipfile

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from qawrench.check import cli  # noqa: E402

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
DEMO = HERE.parents[1] / "examples" / "check"


@pytest.fixture
def demo(tmp_path, monkeypatch):
    """A private copy of examples/check, with the working directory inside it."""
    target = tmp_path / "artifacts"
    shutil.copytree(DEMO, target)
    monkeypatch.chdir(target)
    return target


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """An empty working directory; ``put(name, ...)`` copies fixture files into it."""
    monkeypatch.chdir(tmp_path)

    def put(*names, into="."):
        folder = tmp_path / into
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            shutil.copy(FIXTURES / name, folder / Path(name).name)
        return folder

    put.root = tmp_path
    return put


@pytest.fixture
def check(capsys):
    """Run ``qawrench check`` in-process; return (exit code, stdout, stderr)."""
    def run(*argv):
        try:
            code = cli.main(list(argv))
        except SystemExit as exc:  # argparse usage errors and --help
            code = exc.code
        captured = capsys.readouterr()
        return code, captured.out, captured.err
    return run


@pytest.fixture
def check_json(check):
    """Run with --format json and return (exit code, parsed document)."""
    def run(*argv):
        code, out, err = check(*argv, "--format", "json")
        assert err == "", err
        return code, json.loads(out)
    return run


def rule_ids(document, file=None):
    return sorted(f["ruleId"] for f in document["findings"] if file is None or f["file"] == file)


def write_json(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")
    return path


def demo_json(relative: str):
    return json.loads((DEMO / relative).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- builders

def playwright_test(title, statuses, *, expected="passed", outcome=None, project="chromium"):
    """One Playwright 1.56.1 spec with one test; ``statuses`` are the attempts in order."""
    if outcome is None:
        if not statuses:
            outcome = "skipped"
        elif statuses[-1] == "passed":
            outcome = "flaky" if len(statuses) > 1 else "expected"
        elif statuses[-1] == expected:
            outcome = "expected"
        else:
            outcome = "unexpected"
    results = [{"workerIndex": 0, "parallelIndex": 0, "status": status, "duration": 100 + i, "retry": i,
                "startTime": "2026-11-16T09:00:00.000Z", "errors": [], "stdout": [], "stderr": [],
                "attachments": []} for i, status in enumerate(statuses)]
    return {"title": title, "ok": outcome != "unexpected", "tags": [], "id": f"id-{title}", "file": "a.spec.ts",
            "line": 1, "column": 1,
            "tests": [{"timeout": 30000, "annotations": [], "expectedStatus": expected, "projectName": project,
                       "projectId": project, "results": results, "status": outcome}]}


def playwright_report(*specs, version="1.56.1", errors=()):
    stats = dict.fromkeys(("expected", "unexpected", "flaky", "skipped"), 0)
    for spec in specs:
        stats[spec["tests"][0]["status"]] += 1
    projects = sorted({spec["tests"][0]["projectId"] for spec in specs}) or ["chromium"]
    return {
        "config": {"version": version, "rootDir": "/synthetic", "projects": [
            {"id": p, "name": p, "repeatEach": 1, "retries": 2, "metadata": {}, "outputDir": "/synthetic/out",
             "testDir": "/synthetic/tests", "testIgnore": [], "testMatch": ["**/*.spec.ts"], "timeout": 30000}
            for p in projects]},
        "suites": [{"title": "a.spec.ts", "file": "a.spec.ts", "line": 0, "column": 0, "specs": list(specs),
                    "suites": []}],
        "errors": list(errors),
        "stats": {"startTime": "2026-11-16T09:00:00.000Z", "duration": 1000, **stats},
    }


TRACE_CONTEXT = {"version": 8, "type": "context-options", "origin": "testRunner", "browserName": "chromium",
                 "platform": "linux", "wallTime": 1731747600000, "monotonicTime": 0, "sdkLanguage": "javascript",
                 "options": {}, "title": "SYNTHETIC test trace"}


def ndjson(events) -> bytes:
    return b"".join(json.dumps(event, separators=(",", ":")).encode() + b"\n" for event in events)


def make_trace(path: Path, *, frames=("f1.jpeg",), present=("f1.jpeg",), payload=b"x" * 2000,
               compression=zipfile.ZIP_DEFLATED, trace_bytes=None) -> Path:
    """A small version-8 trace ZIP written with the standard zipfile module."""
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [TRACE_CONTEXT] + [{"type": "screencast-frame", "pageId": "page@1", "sha1": name, "width": 10,
                                 "height": 10, "timestamp": i} for i, name in enumerate(frames)]
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        archive.writestr("trace.trace", ndjson(events) if trace_bytes is None else trace_bytes)
        archive.writestr("trace.network", b"")
        for name in present:
            archive.writestr(f"resources/{name}", payload)
    return path


def make_lhr(*, lcp=2000, tbt=200, cls=0.01, version="12.8.2", url="https://site.example.test/",
             form_factor="mobile", runtime_error=None):
    lhr = copy.deepcopy(demo_json("lighthouse/lhr-1.json"))
    lhr["lighthouseVersion"] = version
    lhr["finalUrl"] = lhr["finalDisplayedUrl"] = lhr["requestedUrl"] = lhr["mainDocumentUrl"] = url
    lhr["configSettings"]["formFactor"] = form_factor
    lhr["audits"]["largest-contentful-paint"]["numericValue"] = lcp
    lhr["audits"]["total-blocking-time"]["numericValue"] = tbt
    lhr["audits"]["cumulative-layout-shift"]["numericValue"] = cls
    if runtime_error is not None:
        lhr["runtimeError"] = runtime_error
    return lhr
