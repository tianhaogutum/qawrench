"""``qawrench check`` command line: formats, exit codes, selection, the README demo."""
import json
import re

import pytest

from qawrench import __version__, cli as dispatcher
from qawrench.check.cli import parse_size
from qawrench.check.registry import AREAS, CHECKS

from .conftest import rule_ids, write_json, playwright_report, playwright_test

SUMMARY = re.compile(r"^\d+ artifacts? · \d+ errors? · \d+ warnings? · \d+ info$", re.MULTILINE)


def test_help_uses_program_name(check):
    code, out, _ = check("--help")
    assert code == 0
    assert "usage: qawrench check" in out
    assert "GITHUB_STEP_SUMMARY" in out


def test_dispatcher_routes_to_check(demo, capsys):
    assert dispatcher.main(["check", "--color", "never"]) == 1
    assert SUMMARY.search(capsys.readouterr().out)


def test_list_checks_names_every_check_by_area(check):
    code, out, _ = check("--list-checks")
    assert code == 0
    for area in AREAS:
        assert f"\n{area}:\n" in out
    for item in CHECKS:
        assert re.search(rf"^  {re.escape(item.name)}\s", out, re.MULTILINE), item.name
    assert "not run by check: needs a before/after pair" in out
    assert "needs qawrench[parquet]" in out


def test_readme_demo_output(demo, check):
    """The lines shown (illustratively) in assets/check-demo.svg and the README."""
    code, out, err = check("--color", "never")
    assert code == 1 and err == ""
    assert out.startswith("qawrench check  .  (8 artifacts: 1 Playwright JSON report, 1 HAR file, "
                          "1 Playwright trace, 1 axe-core results, 3 Lighthouse reports, 1 lychee report; "
                          "8 files inspected)\n")
    for area in ("Tests & flakiness", "Accessibility & performance", "Pages & links"):
        assert f"\n{area}\n" in out
    expected = [
        r"error  flaky/failed +search\.spec\.ts › search › shows suggestions \[chromium\]: "
        r"failed on all 3 attempts \(failed, timedOut, failed\)$",
        r"warn   flaky/flaky +checkout\.spec\.ts › checkout › applies a coupon \[chromium\]: "
        r"failed, then passed on retry 1 \(attempts: failed, passed\)$",
        r"warn   har/ambiguous-replay +GET https://shop\.example\.test/api/stock\?… recorded 2 times with "
        r"different responses \(content differs\); replay serves only one$",
        r"warn   trace-budget/missing-resources +2 references to missing resources \(2 screenshot frames\); "
        r"Trace Viewer will show gaps$",
        r"warn   a11y/violations +3 violations on 1 page \(1 critical, 2 serious\): color-contrast ×2, "
        r"image-alt ×1; no baseline to tell new ones from existing ones$",
        r"warn   lighthouse/noisy +https://shop\.example\.test/ \(mobile\): 3 runs: TBT varies more than 10% "
        r"of the median; TBT 210 ms \(MAD 60 ms, range 150 ms–420 ms\);",
        r"error  links/http-failure +https://docs\.example\.test/retries: Network error \(HTTP 404\)  line 5$",
        r"warn   links/timeout +https://downloads\.example\.test/shop-cli\.tar\.gz: Timeout  line 3$",
    ]
    for pattern in expected:
        assert re.search(r"^    " + pattern, out, re.MULTILINE), pattern
    assert "\n8 artifacts · 2 errors · 7 warnings · 5 info\n" in out
    assert "\x1b[" not in out


def test_demo_json_document(demo, check_json):
    code, document = check_json()
    assert code == 1
    assert document["format"] == "qawrench.check/v1"
    assert document["tool"] == {"name": "qawrench", "version": __version__}
    assert document["summary"] == {"artifacts": 8, "inspected": 8, "error": 2, "warning": 7, "info": 5}
    assert document["failOn"] == "error" and document["failed"] is True
    areas = {f["ruleId"]: f["area"] for f in document["findings"]}
    assert areas["flaky/flaky"] == "Tests & flakiness"
    assert areas["lighthouse/noisy"] == "Accessibility & performance"
    assert areas["links/http-failure"] == "Pages & links"
    trace = next(a for a in document["artifacts"] if a["kind"] == "trace")
    assert trace["copies"] == 0 and trace["findings"] == 2


def test_markdown_for_step_summary(demo, check):
    code, out, err = check("--format", "markdown")
    assert code == 1 and err == ""
    assert out.startswith("## qawrench check\n")
    assert "**Failed** (fail on: error) · 2 errors · 7 warnings · 5 info · 8 artifacts checked" in out
    assert "### Tests & flakiness" in out and "### Pages & links" in out
    assert "| **error** | `links/http-failure` | `docs/testing.md:5` |" in out
    assert "<details><summary>" in out
    assert "\x1b[" not in out


def test_markdown_escapes_table_breakers(workdir, check):
    write_json(workdir.root / "results.json", playwright_report(
        playwright_test("pipe | <b>bold</b> @someone", ["failed", "passed"])))
    _, out, _ = check("--format", "markdown")
    row = next(line for line in out.splitlines() if "flaky/flaky" in line)
    assert row.count(" | ") == 3  # still four cells
    assert "<b>" not in row and "\\<b\\>" in row
    assert "@someone" not in row


def test_sarif_document(demo, check):
    code, out, _ = check("--format", "sarif")
    document = json.loads(out)
    assert code == 1
    assert document["version"] == "2.1.0"
    run = document["runs"][0]
    assert run["tool"]["driver"]["name"] == "qawrench"
    results = {r["ruleId"]: r for r in run["results"]}
    located = results["links/http-failure"]["locations"][0]["physicalLocation"]
    assert located["artifactLocation"] == {"uri": "docs/testing.md", "uriBaseId": "%SRCROOT%"}
    assert located["region"] == {"startLine": 5}
    assert results["flaky/failed"]["level"] == "error"
    assert results["har/summary"]["level"] == "note"
    ids = [rule["id"] for rule in run["tool"]["driver"]["rules"]]
    assert len(ids) == len(set(ids))
    assert run["invocations"][0]["executionSuccessful"] is True


@pytest.mark.parametrize("fail_on, expected", [("error", 1), ("warning", 1), ("never", 0)])
def test_fail_on(demo, check, fail_on, expected):
    assert check("--fail-on", fail_on, "--color", "never")[0] == expected


def test_warnings_only_pass_by_default(demo, check):
    code, _, _ = check("--only", "a11y,lighthouse")
    assert code == 0
    assert check("--only", "a11y,lighthouse", "--fail-on", "warning")[0] == 1


def test_only_and_skip(demo, check_json):
    _, document = check_json("--only", "har")
    assert {f["check"] for f in document["findings"]} == {"har"}
    assert document["checks"] == ["har"]
    _, document = check_json("--skip", "flaky", "--skip", "links,a11y")
    assert not {"flaky", "links", "a11y"} & {f["check"] for f in document["findings"]}


def test_usage_errors_exit_2(demo, check):
    code, _, err = check("--only", "nope")
    assert code == 2 and "unknown check(s): nope" in err
    code, _, err = check("missing-dir")
    assert code == 2 and "no such file or directory: missing-dir" in err
    code, _, err = check("--only", "har", "--skip", "har")
    assert code == 2 and "leave no check" in err
    assert check("--trace-budget", "lots")[0] == 2
    assert check("--source-root", "missing-dir")[0] == 2


def test_empty_folder_is_clean(workdir, check):
    code, out, _ = check("--color", "never")
    assert code == 0
    assert "No test artifacts found" in out
    assert out.rstrip().endswith("0 artifacts · 0 errors · 0 warnings · 0 info")


def test_default_path_is_current_directory(demo, check_json):
    _, document = check_json("--only", "har")
    assert document["paths"] == ["."]


def test_trace_budget_option(demo, check_json):
    _, document = check_json("--only", "trace-budget", "--trace-budget", "50kB")
    assert "trace-budget/over-budget" in rule_ids(document)
    _, document = check_json("--only", "trace-budget", "--trace-budget", "1MB")
    assert "trace-budget/over-budget" not in rule_ids(document)


@pytest.mark.parametrize("text, expected", [
    ("500000", 500_000), ("500k", 500_000), ("25MB", 25_000_000), ("24MiB", 24 * 2 ** 20), ("1.5 GB", 1_500_000_000),
])
def test_parse_size(text, expected):
    assert parse_size(text) == expected


def test_verbose_shows_hints_and_pointers(demo, check):
    _, out, _ = check("--color", "never", "-v")
    assert "/log/entries/2" in out
    assert "-> entries 2, 3; keep one with `qawrench har select-requests`" in out
    assert "without findings of their own" in out


def test_check_writes_nothing(demo, check):
    before = {str(p): p.stat().st_mtime_ns for p in demo.rglob("*")}
    for fmt in ("text", "json", "sarif", "markdown"):
        check("--format", fmt)
    assert {str(p): p.stat().st_mtime_ns for p in demo.rglob("*")} == before


def test_color_always_uses_ansi(demo, check):
    _, out, _ = check("--color", "always")
    assert "\x1b[1;31merror" in out
