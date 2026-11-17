"""`qawrench msw-contract` command line: options, exit codes, runtime errors and real handler modules."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import pytest

import qawrench
from qawrench.msw_contract import check

from .support import (CONTRACT, EXAMPLES, FIXTURES, HANDLERS, ROOT, npm_package_installed, requires_msw,
                      requires_node, requires_node_only, run_cli)


@pytest.fixture
def fixture_copy_dir():
    # Inside fixtures/, so `msw` resolves from the fixtures' node_modules like the other handlers.
    directory = Path(tempfile.mkdtemp(prefix="tmp-", dir=FIXTURES))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _cases(path: Path, *cases) -> Path:
    path.write_text(json.dumps(list(cases)), encoding="utf-8")
    return path


@pytest.mark.parametrize("args", [
    [], ["--format", "markdown"], ["--unknown"], ["--handlers", "x", "--schema", "x", "--cases", "x", "--timeout", "NaN"],
    ["--handlers", "x", "--schema", "x", "--cases", "x", "--sample-bytes", "1.5"], ["positional"],
])
def test_cli_rejects_malformed_options_without_a_traceback(args):
    result = run_cli(*args)
    assert result.returncode == 2
    assert result.stderr.startswith("qawrench msw-contract:")
    assert "Traceback" not in result.stderr


def test_cli_help_and_version():
    result = run_cli("--help")
    assert result.returncode == 0
    assert "usage: qawrench msw-contract" in result.stdout
    assert "explicit request" in result.stdout
    for flag in ("--handlers", "--schema", "--cases", "--base-url", "--api-root", "--export", "--timeout",
                 "--max-body-bytes", "--sample-bytes", "--format", "--output"):
        assert flag in result.stdout
    version = run_cli("--version")
    assert version.returncode == 0 and version.stdout == f"{qawrench.__version__}\n"


def test_missing_node_is_a_one_line_configuration_error(tmp_path):
    cases = _cases(tmp_path / "cases.json", {"url": "http://example.test/pets/1"})
    env = {**os.environ, "PATH": str(tmp_path)}
    result = run_cli("--handlers", HANDLERS, "--schema", CONTRACT, "--cases", cases, env=env)
    assert result.returncode == 2, result.stderr
    assert result.stderr.startswith("qawrench msw-contract: Node.js was not found on PATH")
    assert result.stderr.count("\n") == 1 and result.stdout == ""


@requires_node_only
def test_unresolvable_msw_is_a_one_line_configuration_error(tmp_path):
    if npm_package_installed("msw", tmp_path):
        pytest.skip("an msw install above the temporary directory would be found")
    handlers = tmp_path / "handlers.mjs"
    handlers.write_text("export const handlers = [];\n", encoding="utf-8")
    cases = _cases(tmp_path / "cases.json", {"url": "http://example.test/pets/1"})
    result = run_cli("--handlers", handlers, "--schema", CONTRACT, "--cases", cases, cwd=tmp_path)
    assert result.returncode == 2, result.stderr
    assert result.stderr.startswith("qawrench msw-contract: cannot resolve the 'msw' package")
    assert result.stderr.count("\n") == 1


_WITHOUT = ("import sys\nfor name in {blocked!r}: sys.modules[name] = None\n"
            "from qawrench.cli import main\nraise SystemExit(main({argv!r}))\n")


@pytest.mark.parametrize("blocked", ["yaml", "jsonschema"])
def test_missing_openapi_extra_exits_2_with_install_hint(blocked, tmp_path):
    argv = ["msw-contract", "--handlers", str(HANDLERS), "--schema", str(CONTRACT), "--cases",
            str(_cases(tmp_path / "cases.json", {"url": "http://example.test/pets/1"}))]
    result = subprocess.run([sys.executable, "-c", _WITHOUT.format(blocked=[blocked], argv=argv)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 2
    assert result.stderr.startswith(f"qawrench msw-contract needs {blocked}: pipx install 'qawrench[openapi]'")
    assert "Traceback" not in result.stderr


@requires_node("msw2")
@pytest.mark.parametrize("module,export_name", [("msw2.mjs", "legacyHandlers"), ("default.cjs", "handlers"),
                                                ("typed.ts", "handlers")])
def test_msw2_handlers_commonjs_object_export_and_native_typescript_are_executable(module, export_name):
    report = check(handlers_path=FIXTURES / module, export_name=export_name, schema_path=CONTRACT, cwd=ROOT,
                   cases=[{"url": "http://example.test/pets/1"}])
    assert report["results"][0]["ok"] is True, report


@requires_msw
def test_relative_msw_handlers_use_the_explicit_base_and_never_guess_a_request_origin():
    options = {"handlers_path": FIXTURES / "relative.mjs", "schema_path": CONTRACT, "cwd": ROOT}
    relative = check(**options, base_url="https://example.test/", cases=[{"url": "/pets/1"}])
    assert relative["results"][0]["ok"] is True, relative
    other = check(**options, base_url="https://other.test/", cases=[{"url": "https://example.test/pets/1"}])
    assert other["results"][0]["issues"][0]["code"] == "unhandled-request"


@requires_msw
def test_cli_json_output_is_machine_readable_and_drift_exits_1(fixture_copy_dir, tmp_path):
    handlers = fixture_copy_dir / "handlers.mjs"
    shutil.copyfile(EXAMPLES / "handlers.mjs", handlers)
    result = run_cli("--handlers", handlers, "--schema", EXAMPLES / "petstore-3.0.yaml",
                     "--cases", EXAMPLES / "cases.json", "--api-root", "/v1", "--format", "json")
    assert result.returncode == 1, result.stderr
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report["summary"] == {"total": 3, "passed": 2, "failed": 1}
    assert report["results"][1]["issues"][0]["path"] == "/id"
    output = tmp_path / "report.txt"
    text = run_cli("--handlers", handlers, "--schema", EXAMPLES / "petstore-3.0.yaml", "--cases",
                   EXAMPLES / "cases.json", "--api-root", "/v1", "--output", output)
    assert text.returncode == 1 and text.stdout == ""
    assert output.read_text(encoding="utf-8").splitlines()[:4] == [
        "2/3 examples passed; 1 failed (explicit examples only)",
        "PASS list-pets: GET http://petstore.swagger.io/v1/pets?limit=2 -> 200 application/json",
        "FAIL stale-id: GET http://petstore.swagger.io/v1/pets/1 -> 200 application/json",
        '  schema /id: must be integer; actual "1"']


@requires_msw
def test_fixed_example_handler_passes(fixture_copy_dir):
    handlers = fixture_copy_dir / "handlers.mjs"
    source = (EXAMPLES / "handlers.mjs").read_text(encoding="utf-8")
    handlers.write_text(source.replace("id: params.petId", "id: Number(params.petId)"), encoding="utf-8")
    result = run_cli("--handlers", handlers, "--schema", EXAMPLES / "petstore-3.0.yaml",
                     "--cases", EXAMPLES / "cases.json", "--api-root", "/v1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("3/3 examples passed; 0 failed")


@requires_msw
@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_sigterm_cancels_cli_work_with_a_conventional_exit_code(tmp_path):
    cases = _cases(tmp_path / "cases.json", {"url": "http://example.test/pets/1?scenario=timeout"})
    child = subprocess.Popen([sys.executable, "-m", "qawrench", "msw-contract", "--handlers", str(HANDLERS),
                              "--schema", str(CONTRACT), "--cases", str(cases), "--timeout", "60000"],
                             cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(3)  # Past interpreter start-up, inside the hanging handler case.
        child.send_signal(signal.SIGTERM)
        _, stderr = child.communicate(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
    assert child.returncode == 143, stderr
    assert "Interrupted by SIGTERM" in stderr
