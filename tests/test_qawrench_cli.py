"""Top-level `qawrench` dispatcher: overview, version, routing and error paths."""
from pathlib import Path
import subprocess
import sys

import pytest

import qawrench
from qawrench import cli

ROOT = Path(__file__).resolve().parents[1]

# Commands whose Python subpackage ships in this repository (`check` is the hero command).
TOOLS = ["check", "flaky", "har", "trace-budget", "statewatch", "msw-contract", "a11y", "lighthouse",
         "mermaid-paths", "page-diff", "structured-data", "links", "frontier", "crawlee-export"]
# Every merged tool is built on argparse: `--help` prints a "usage: qawrench <tool>" line.
ARGPARSE_TOOLS = [tool for tool in TOOLS if tool != "check"]
# `check` keeps its own flags; every merged tool prints the bare package version.
VERSIONED = ARGPARSE_TOOLS
SUBCOMMANDS = {
    "har": ["export-inventory", "select-requests", "plan-selection", "embed-forms", "plan", "apply",
            "preflight"],
    "flaky": ["import", "report", "export", "export-ledger", "import-ledger", "prune", "verify-archive",
              "verify-run", "list-archives", "list-runs", "compare-runs", "test-history"],
    "a11y": ["draft-exception", "baseline", "diff", "migration"],
    "trace-budget": ["inspect", "repack", "compare"],
    "mermaid-paths": ["review", "inspect", "build", "styles"],
    # Single-parser tools with one hand-routed secondary mode.
    "lighthouse": ["save-baseline"],
    "frontier": ["verify-witnesses"],
}
PACKAGES = {
    "flaky": "qawrench.flaky", "har": "qawrench.har", "trace-budget": "qawrench.trace_budget",
    "a11y": "qawrench.a11y", "lighthouse": "qawrench.lighthouse", "page-diff": "qawrench.page_diff",
    "structured-data": "qawrench.structured_data", "links": "qawrench.links",
    "frontier": "qawrench.frontier", "crawlee-export": "qawrench.crawlee_export",
    "statewatch": "qawrench.statewatch", "msw-contract": "qawrench.msw_contract",
    "mermaid-paths": "qawrench.mermaid_paths",
}
LEGACY_NAMES = [
    ("har-replay-doctor", "har"), ("retry-history-ledger", "flaky"), ("a11y-baseline-bridge", "a11y"),
    ("crawlee-parquet-export", "crawlee-export"), ("structured-data-parity", "structured-data"),
    ("trace-size-budget", "trace-budget"), ("lighthouse-noise-gate", "lighthouse"),
    ("stable-page-diff", "page-diff"), ("crawl-frontier-audit", "frontier"),
    ("verify-robots-witnesses", "frontier verify-witnesses"), ("lychee-sarif-map", "links"),
    ("pytest-statewatch", "statewatch"), ("msw-contract-check", "msw-contract"),
]


def run(*args, module=("-m", "qawrench")):
    return subprocess.run([sys.executable, *module, *args], capture_output=True, text=True, timeout=60)


def _main(argv, capsys):
    try:
        code = cli.main(argv)
    except SystemExit as exc:
        code = exc.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize("args", [[], ["--help"], ["-h"], ["help"]])
def test_overview_lists_every_group_and_command(args, capsys):
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"qawrench {qawrench.__version__} ")
    for group in cli.GROUPS:
        assert f"\n{group}:\n" in out
    for name in TOOLS:
        assert f"\n  {name} " in out


def test_command_table_shape():
    groups = {group: [c.name for c in cli.COMMANDS if c.group == group] for group in cli.GROUPS}
    assert groups == {
        "Check": ["check"],
        "Tests & flakiness": ["flaky", "har", "trace-budget", "statewatch", "msw-contract"],
        "Accessibility & performance": ["a11y", "lighthouse", "mermaid-paths"],
        "Pages & links": ["page-diff", "structured-data", "links"],
        "Crawling": ["frontier", "crawlee-export"],
    }
    assert {c.name: c.module for c in cli.COMMANDS} == {
        "check": "qawrench.check.cli",
        **{name: f"{package}.cli" for name, package in PACKAGES.items()},
    }
    assert all(c.group in cli.GROUPS and c.summary for c in cli.COMMANDS)
    assert set(cli.BY_NAME) == {c.name for c in cli.COMMANDS}
    assert {c.name: c.extra for c in cli.COMMANDS if c.extra} == {
        "page-diff": "html", "structured-data": "structured", "frontier": "frontier",
        "crawlee-export": "parquet", "msw-contract": "openapi",
    }
    assert all(bool(c.requires) == bool(c.extra) for c in cli.COMMANDS)


def test_extras_are_declared_in_pyproject():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for extra in {c.extra for c in cli.COMMANDS if c.extra} | {"all", "test", "statewatch"}:
        assert f"\n{extra} = [" in text, extra


def test_shipped_tools_are_installed():
    for name in ARGPARSE_TOOLS:
        assert cli._installed(cli.BY_NAME[name]), name


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version(flag, capsys):
    assert cli.main([flag]) == 0
    assert capsys.readouterr().out == f"qawrench {qawrench.__version__}\n"


def _assert_tool_help(tool, out):
    assert f"qawrench {tool}" in out
    if tool in ARGPARSE_TOOLS:
        assert f"usage: qawrench {tool}" in out


@pytest.mark.parametrize("tool", ARGPARSE_TOOLS)
def test_tool_help_uses_new_program_name(tool):
    result = run(tool, "--help")
    assert result.returncode == 0, result.stderr
    _assert_tool_help(tool, result.stdout)


@pytest.mark.parametrize("tool", ARGPARSE_TOOLS)
def test_help_subcommand_routes_to_tool_help(tool, capsys):
    code, out, _ = _main(["help", tool], capsys)
    assert code == 0
    _assert_tool_help(tool, out)


@pytest.mark.parametrize("tool", VERSIONED)
def test_tool_version_is_package_version(tool, capsys):
    code, out, _ = _main([tool, "--version"], capsys)
    assert code == 0
    assert out == f"{qawrench.__version__}\n"


@pytest.mark.parametrize("command,sub", [(c, s) for c, subs in SUBCOMMANDS.items() for s in subs])
def test_subcommand_help_uses_new_program_name(command, sub, capsys):
    code, out, err = _main([command, sub, "--help"], capsys)
    assert code == 0, err
    assert f"usage: qawrench {command} {sub}" in out


@pytest.mark.parametrize("tool,package", sorted(PACKAGES.items()))
def test_subpackage_main_routes_through_dispatcher(tool, package):
    result = run("--help", module=("-m", package))
    assert result.returncode == 0, result.stderr
    assert f"usage: qawrench {tool}" in result.stdout


def test_frontier_help_mentions_verify_witnesses(capsys):
    code, out, _ = _main(["frontier", "--help"], capsys)
    assert code == 0 and "qawrench frontier verify-witnesses" in out


@pytest.mark.parametrize("tool", ["har", "flaky", "a11y", "trace-budget", "mermaid-paths"])
def test_tool_exit_codes_pass_through(tool):
    bad = run(tool, "no-such-subcommand")
    assert bad.returncode == 2 and f"usage: qawrench {tool}" in bad.stderr
    assert "Traceback" not in bad.stderr


def test_unknown_command_suggests_close_match(capsys):
    assert cli.main(["flakky"]) == 2
    err = capsys.readouterr().err
    assert "unknown command 'flakky'" in err and "did you mean 'flaky'?" in err


def test_unknown_legacy_typo_suggests_new_command(capsys):
    assert cli.main(["har-replay-docter"]) == 2
    assert "did you mean 'har'?" in capsys.readouterr().err


@pytest.mark.parametrize("legacy,new", LEGACY_NAMES)
def test_unknown_legacy_name_points_to_new_command(legacy, new, capsys):
    assert cli.main([legacy]) == 2
    assert f"{legacy} is now 'qawrench {new}'" in capsys.readouterr().err


def test_unknown_command_without_match(capsys):
    assert cli.main(["zzzzzz"]) == 2
    err = capsys.readouterr().err
    assert "unknown command 'zzzzzz'" in err and "did you mean" not in err


def test_unknown_option(capsys):
    assert cli.main(["--frobnicate"]) == 2
    assert "unknown option '--frobnicate'" in capsys.readouterr().err


def test_missing_module_reports_not_available(monkeypatch, capsys):
    ghost = cli.Command("ghost", "qawrench.ghost_tool.cli", "not shipped", "Check")
    monkeypatch.setitem(cli.BY_NAME, "ghost", ghost)
    monkeypatch.setattr(cli, "COMMANDS", (*cli.COMMANDS, ghost))
    overview = cli.overview()
    assert "ghost" in overview and "(coming soon)" in overview
    assert cli.main(["ghost", "--help"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("qawrench: command 'ghost' is not available") and "Traceback" not in err


def test_missing_optional_dependency_prints_install_hint(monkeypatch, capsys):
    command = cli.Command("ghost", "qawrench.links.cli", "needs a dependency", "Check",
                          requires=("ghostdep",), extra="ghostextra")
    monkeypatch.setitem(cli.BY_NAME, "ghost", command)

    def missing(name):
        raise ModuleNotFoundError("No module named 'ghostdep.sub'", name="ghostdep.sub")

    monkeypatch.setattr(cli.importlib, "import_module", missing)
    assert cli.main(["ghost", "--help"]) == 2
    err = capsys.readouterr().err
    assert err == ("qawrench ghost needs ghostdep: pipx install 'qawrench[ghostextra]' "
                   "(or pip install 'qawrench[ghostextra]')\n")


def test_unrelated_module_error_is_not_masked(monkeypatch):
    command = cli.Command("ghost", "qawrench.links.cli", "needs a dependency", "Check",
                          requires=("ghostdep",), extra="ghostextra")
    monkeypatch.setitem(cli.BY_NAME, "ghost", command)

    def missing(name):
        raise ModuleNotFoundError("No module named 'other'", name="other")

    monkeypatch.setattr(cli.importlib, "import_module", missing)
    with pytest.raises(ModuleNotFoundError):
        cli.main(["ghost"])


# Simulate an install without the extra: a None entry in sys.modules makes the import fail.
_WITHOUT = ("import sys\nfor name in {blocked!r}: sys.modules[name] = None\n"
            "from qawrench.cli import main\nraise SystemExit(main({argv!r}))\n")


@pytest.mark.parametrize("blocked,argv,hint", [
    (["bs4"], ["page-diff", "a.html", "b.html", "--rules", "rules.json"],
     "qawrench page-diff needs bs4: pipx install 'qawrench[html]'"),
    (["extruct"], ["structured-data", "a.html", "b.html", "--base-url", "https://example.test/"],
     "qawrench structured-data needs extruct: pipx install 'qawrench[structured]'"),
    (["w3lib"], ["frontier", "requests.jsonl"],
     "qawrench frontier needs w3lib: pipx install 'qawrench[frontier]'"),
    (["yaml"], ["msw-contract", "--handlers", "handlers.mjs", "--schema", "openapi.yaml", "--cases", "cases.json"],
     "qawrench msw-contract needs yaml: pipx install 'qawrench[openapi]'"),
])
def test_missing_extra_exits_2_with_one_line_hint(blocked, argv, hint, tmp_path):
    result = subprocess.run([sys.executable, "-c", _WITHOUT.format(blocked=blocked, argv=argv)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert result.returncode == 2
    assert result.stderr.startswith(hint) and result.stderr.count("\n") == 1
    assert "Traceback" not in result.stderr


def test_crawlee_export_without_pyarrow_exits_2_with_hint(tmp_path):
    examples = ROOT / "examples" / "crawlee_export"
    argv = ["crawlee-export", str(examples / "dataset"), "--schema", str(examples / "schema.json"),
            "--out", str(tmp_path / "out.parquet")]
    result = subprocess.run([sys.executable, "-c", _WITHOUT.format(blocked=["pyarrow"], argv=argv)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert result.returncode == 2
    assert "qawrench crawlee-export needs pyarrow: pipx install 'qawrench[parquet]'" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "out.parquet").exists()


@pytest.mark.parametrize("name", TOOLS)
def test_shipped_command_is_listed_without_coming_soon(name, capsys):
    if not cli._installed(cli.BY_NAME[name]):
        pytest.skip(f"{name} has not landed yet")
    assert cli.main([]) == 0
    line = next(line for line in capsys.readouterr().out.splitlines() if line.startswith(f"  {name} "))
    assert "(coming soon)" not in line


def test_statewatch_plugin_entry_point_is_declared():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '[project.entry-points.pytest11]\nstatewatch = "qawrench.statewatch.plugin"' in text
