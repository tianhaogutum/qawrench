"""The statewatch command and plugin inside qawrench: names, version, lazy dependencies."""
from __future__ import annotations

import subprocess
import sys

import pytest

import qawrench
from qawrench.statewatch.cli import main


def test_help_uses_the_qawrench_program_name(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "usage: qawrench statewatch" in out
    assert "python -m pytest --statewatch policy.json" in out


def test_version_is_the_bare_package_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"{qawrench.__version__}\n"


def test_errors_are_prefixed_with_the_command_name(tmp_path, capsys):
    assert main([str(tmp_path / "missing.json")]) == 2
    assert capsys.readouterr().err.startswith("qawrench statewatch: cannot read ")


def test_report_with_missing_fields_is_rejected(tmp_path, capsys):
    path = tmp_path / "report.json"
    path.write_text('{"schema_version": 1, "tests": []}')
    assert main([str(path)]) == 2
    assert "tests_with_net_changes" in capsys.readouterr().err


@pytest.mark.parametrize("module", [("-m", "qawrench.statewatch"), ("-m", "qawrench", "statewatch")])
def test_module_entry_points_route_through_the_dispatcher(module):
    result = subprocess.run([sys.executable, *module, "--help"], capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0, result.stderr
    assert "usage: qawrench statewatch" in result.stdout


def test_plugin_and_cli_import_no_observation_dependency():
    # The pytest11 entry point loads the plugin in every pytest run once qawrench is installed.
    code = (
        "import sys\n"
        "import qawrench.statewatch.plugin, qawrench.statewatch.cli, qawrench.statewatch.adapters\n"
        "loaded = sorted({'deepdiff', 'filelock', 'pytest_replay'} & set(sys.modules))\n"
        "assert not loaded, loaded\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_missing_dependency_is_a_one_line_usage_error(suite):
    # A None entry in sys.modules makes `import deepdiff` fail like an install without the extra.
    conftest = "import sys\nsys.modules['deepdiff'] = None\n"
    result, report, events, root = suite("def test_ok(): pass\n", {"cwd": True}, conftest=conftest)
    assert result.returncode == 4, result.stdout + result.stderr
    assert ("qawrench statewatch needs deepdiff: pipx install 'qawrench[statewatch]' "
            "(or pip install 'qawrench[statewatch]')") in result.stderr
    assert "Traceback" not in result.stderr
    assert report is None and not events
    assert not (root / "artifacts").exists()
