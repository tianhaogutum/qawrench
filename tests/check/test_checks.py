"""Each check against small synthetic artifacts, through the public discover/run_checks API."""
import copy
import json
import zipfile

import pytest

from qawrench.check import CheckOptions, discover, run_checks, select
from qawrench.check import checks as check_module
from qawrench.check import registry
from qawrench.check.model import Finding

from .conftest import (demo_json, make_lhr, make_trace, playwright_report, playwright_test,
                       write_json)


def run(paths=("."), only=(), **options):
    paths = [paths] if isinstance(paths, str) else list(paths)
    return run_checks(discover(paths), select(only), CheckOptions(**options), requested=only)


def by_rule(findings, rule_id):
    return [finding for finding in findings if finding.rule_id == rule_id]


# --------------------------------------------------------------------------- flaky

def test_flaky_demo_report(demo):
    findings = run("test-results/results.json", only=["flaky"])
    flaky = by_rule(findings, "flaky/flaky")
    assert [f.subject for f in flaky] == ["account.spec.ts › account › signs in [chromium]",
                                          "checkout.spec.ts › checkout › applies a coupon [chromium]"]
    assert all(f.severity == "warning" and "passed on retry 1" in f.message for f in flaky)
    failed = by_rule(findings, "flaky/failed")
    assert [(f.severity, f.message) for f in failed] == [
        ("error", "failed on all 3 attempts (failed, timedOut, failed)")]
    assert by_rule(findings, "flaky/summary")[0].message == (
        "5 tests: 2 passed, 2 flaky, 1 failed, 0 skipped; 3 needed retries")


def test_flaky_unexpected_pass_interrupted_and_global_errors(workdir):
    report = playwright_report(
        playwright_test("marked fail", ["passed"], expected="failed", outcome="unexpected"),
        playwright_test("cut short", ["interrupted"], outcome="unexpected"),
        errors=[{"message": "global setup failed"}])
    write_json(workdir.root / "results.json", report)
    findings = run(only=["flaky"])
    assert len(by_rule(findings, "flaky/unexpected-pass")) == 1
    assert len(by_rule(findings, "flaky/interrupted")) == 1
    errors = by_rule(findings, "flaky/global-errors")
    assert len(errors) == 1 and "global setup failed" not in errors[0].message


def test_flaky_other_playwright_version_is_info(workdir):
    write_json(workdir.root / "results.json",
               playwright_report(playwright_test("ok", ["passed"]), version="1.40.0"))
    findings = run(only=["flaky"])
    assert [(f.rule_id, f.severity) for f in findings] == [("flaky/unsupported-report", "info")]
    assert "1.56.1" in findings[0].message


def test_flaky_ctrf(workdir):
    workdir("ctrf.json")
    findings = run(only=["flaky"])
    assert [f.subject for f in by_rule(findings, "flaky/flaky")] == ["checkout.spec.ts › checkout › applies a coupon"]
    assert [f.subject for f in by_rule(findings, "flaky/failed")] == ["search.spec.ts › search › shows suggestions"]


def test_junit_gets_a_hint_not_an_analysis(workdir):
    workdir("junit.xml")
    findings = run(only=["flaky"])
    assert [(f.rule_id, f.severity) for f in findings] == [("flaky/junit-not-read", "info")]
    assert "JSON reporter" in findings[0].hint


# --------------------------------------------------------------------------- har

def test_har_demo_recording(demo):
    findings = run("recordings/api.har", only=["har"])
    ambiguous = by_rule(findings, "har/ambiguous-replay")
    assert len(ambiguous) == 1
    assert ambiguous[0].pointer == "/log/entries/2"
    assert ambiguous[0].message.startswith("GET https://shop.example.test/api/stock?… recorded 2 times")
    assert "content differs" in ambiguous[0].message
    volatile = by_rule(findings, "har/volatile-query-key")
    assert [f.message.split("'")[1] for f in volatile] == ["cacheBust"]
    assert by_rule(findings, "har/summary")[0].message == (
        "5 entries; 1 request recorded more than once, 1 with different responses")
    # Query values, bodies and response text never reach the report.
    text = json.dumps([f.to_dict() for f in findings])
    for secret in ("sku=42", "1731747601000", "DEMO10", '\\"stock\\"'):
        assert secret not in text


def test_meaningful_query_key_is_not_volatile(workdir):
    har = demo_json("recordings/api.har")
    first, second = har["log"]["entries"][:2]
    second["response"]["content"]["text"] = '{"items":2,"total":84}'  # different page => meaningful key
    write_json(workdir.root / "api.har", har)
    assert by_rule(run(only=["har"]), "har/volatile-query-key") == []


def test_attached_har_content_is_unsupported_info(workdir):
    workdir("attached.har")
    findings = run(only=["har"])
    assert [(f.rule_id, f.severity) for f in findings] == [("har/unsupported", "info")]
    assert "_file" in findings[0].message


def test_har_zip_is_read(workdir):
    har = demo_json("recordings/api.har")
    with zipfile.ZipFile(workdir.root / "api.har.zip", "w") as archive:
        archive.writestr("api.har", json.dumps(har))
    findings = run(only=["har"])
    assert len(by_rule(findings, "har/ambiguous-replay")) == 1


# --------------------------------------------------------------------------- trace-budget

def test_trace_demo_missing_frames(demo):
    findings = run("test-results", only=["trace-budget"])
    missing = by_rule(findings, "trace-budget/missing-resources")
    assert [f.message for f in missing] == [
        "2 references to missing resources (2 screenshot frames); Trace Viewer will show gaps"]
    size = by_rule(findings, "trace-budget/size")[0].message
    assert size.startswith("51.5 kB, 5 members: resources 98% (screenshots 50.0 kB, network bodies 22 B)")


def test_trace_budget(workdir):
    make_trace(workdir.root / "trace.zip", payload=bytes(range(256)) * 40, compression=zipfile.ZIP_STORED)
    assert by_rule(run(only=["trace-budget"]), "trace-budget/over-budget") == []
    over = by_rule(run(only=["trace-budget"], trace_budget=1000), "trace-budget/over-budget")
    assert len(over) == 1 and "exceeds the 1.0 kB trace budget" in over[0].message


def test_trace_with_broken_events_is_a_warning(workdir):
    make_trace(workdir.root / "trace.zip", trace_bytes=b"{not json\n")
    findings = run(only=["trace-budget"])
    assert [(f.rule_id, f.severity) for f in findings] == [("trace-budget/invalid", "warning")]


def test_trace_of_other_version_is_info(workdir):
    context = {"version": 7, "type": "context-options", "origin": "testRunner", "browserName": "chromium",
               "platform": "linux", "wallTime": 1, "monotonicTime": 0, "options": {}}
    make_trace(workdir.root / "trace.zip", trace_bytes=json.dumps(context).encode() + b"\n", present=())
    findings = run(only=["trace-budget"])
    assert [(f.rule_id, f.severity) for f in findings] == [("trace-budget/unsupported", "info")]


# --------------------------------------------------------------------------- a11y

def test_a11y_without_baseline_summarises(demo):
    findings = run("a11y", only=["a11y"])
    assert [(f.rule_id, f.severity) for f in findings] == [("a11y/violations", "warning")]
    assert findings[0].message.startswith("3 violations on 1 page (1 critical, 2 serious): color-contrast ×2")


def test_a11y_new_violation_against_baseline(workdir):
    from qawrench.a11y.core import make_baseline

    current = demo_json("a11y/axe-home.json")
    before = copy.deepcopy(current)
    before["violations"] = [rule for rule in before["violations"] if rule["id"] != "image-alt"]
    write_json(workdir.root / "a11y" / "axe-home.json", current)
    write_json(workdir.root / "a11y" / "a11y-baseline.json", make_baseline(before, "axe"))
    findings = run(only=["a11y"])
    new = by_rule(findings, "a11y/new-violation")
    assert [(f.severity, f.message, f.subject) for f in new] == [
        ("error", "new critical violation image-alt at #hero > img", "https://shop.example.test/")]
    delta = by_rule(findings, "a11y/baseline-delta")[0].message
    assert delta == "compared with a11y/a11y-baseline.json: 1 new, 0 fixed, 2 unchanged"


def test_a11y_unchanged_against_its_own_baseline(workdir):
    from qawrench.a11y.core import make_baseline

    report = demo_json("a11y/axe-home.json")
    write_json(workdir.root / "axe.json", report)
    write_json(workdir.root / "baseline.json", make_baseline(report, "axe"))
    findings = run(only=["a11y"])
    assert [f.rule_id for f in findings] == ["a11y/baseline-delta"]
    assert "0 new" in findings[0].message


def test_a11y_incomplete_scan_is_not_counted_as_clean(workdir):
    report = demo_json("a11y/axe-home.json")
    report["incomplete"] = [{"id": "color-contrast", "nodes": []}]
    write_json(workdir.root / "axe.json", report)
    findings = run(only=["a11y"])
    assert [(f.rule_id, f.severity) for f in findings] == [("a11y/unsupported-report", "info")]


def test_pa11y_ci(workdir):
    workdir("pa11y-ci.json")
    findings = run(only=["a11y"])
    assert [(f.rule_id, f.severity) for f in findings] == [("a11y/violations", "warning")]
    assert "1 serious" in findings[0].message


# --------------------------------------------------------------------------- lighthouse

def test_lighthouse_demo_is_noisy(demo):
    findings = run("lighthouse", only=["lighthouse"])
    assert [(f.rule_id, f.file) for f in findings] == [("lighthouse/noisy", "lighthouse/lhr-1.json")]
    assert findings[0].subject == "https://shop.example.test/ (mobile)"
    assert findings[0].message == (
        "3 runs: TBT varies more than 10% of the median; TBT 210 ms (MAD 60 ms, range 150 ms–420 ms); "
        "LCP 2.50 s (MAD 100 ms); CLS 0.020 (MAD 0.000)")


def test_lighthouse_stable_and_too_few_runs(workdir):
    for index, lcp in enumerate((2000, 2010, 1990)):
        write_json(workdir.root / "stable" / f"lhr-{index}.json", make_lhr(lcp=lcp, tbt=200))
    for index in range(2):
        write_json(workdir.root / "pair" / f"lhr-{index}.json", make_lhr(url="https://other.example.test/"))
    findings = run(only=["lighthouse"])
    assert sorted(f.rule_id for f in findings) == ["lighthouse/stable", "lighthouse/too-few-runs"]


def test_lighthouse_never_pools_folders(workdir):
    for side, values in (("baseline", (2000, 2000)), ("current", (4000, 4000))):
        for index, lcp in enumerate(values):
            write_json(workdir.root / side / f"lhr-{index}.json", make_lhr(lcp=lcp))
    findings = run(only=["lighthouse"])
    assert sorted(f.file for f in findings) == ["baseline/lhr-0.json", "current/lhr-0.json"]
    assert all(f.rule_id == "lighthouse/too-few-runs" for f in findings)


def test_lighthouse_failed_and_unsupported_runs(workdir):
    write_json(workdir.root / "a.json", make_lhr(runtime_error={"code": "NO_FCP", "message": "x"}))
    write_json(workdir.root / "b.json", make_lhr(version="11.0.0"))
    findings = run(only=["lighthouse"])
    assert [(f.rule_id, f.file, f.severity) for f in findings] == [
        ("lighthouse/runtime-error", "a.json", "warning"), ("lighthouse/unsupported-run", "b.json", "info")]


# --------------------------------------------------------------------------- links

def test_links_demo_mapped_to_lines(demo):
    findings = run(only=["links"])
    located = [(f.rule_id, f.severity, f.file, f.line) for f in findings if f.rule_id != "links/summary"]
    assert located == [("links/timeout", "warning", "docs/install.md", 3),
                       ("links/http-failure", "error", "docs/testing.md", 5)]
    assert by_rule(findings, "links/http-failure")[0].message == (
        "https://docs.example.test/retries: Network error (HTTP 404)")


def test_links_falls_back_to_the_report_folder(demo, monkeypatch):
    monkeypatch.chdir(demo.parent)
    findings = run(["artifacts/lychee.json"], only=["links"])
    assert [f.file for f in by_rule(findings, "links/http-failure")] == ["artifacts/docs/testing.md"]


def test_links_sources_not_found(demo, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "lychee.json").write_bytes((demo / "lychee.json").read_bytes())
    monkeypatch.chdir(elsewhere)
    findings = run(only=["links"])
    assert [(f.rule_id, f.severity) for f in findings] == [("links/sources-not-found", "warning")]
    assert "2 failed links" in findings[0].message
    findings = run(only=["links"], source_root=demo)
    assert len(by_rule(findings, "links/http-failure")) == 1


def test_links_cached_report_is_unsupported(demo):
    report = json.loads((demo / "lychee.json").read_text())
    report["cached"] = 1
    (demo / "lychee.json").write_text(json.dumps(report))
    findings = run(only=["links"])
    assert [(f.rule_id, f.severity) for f in findings] == [("links/unsupported-report", "info")]


# --------------------------------------------------------------------------- crawlee-export

def test_crawlee_dataset_needs_a_schema(workdir):
    write_json(workdir.root / "storage" / "datasets" / "default" / "000000001.json", {"title": "a"})
    findings = run(only=["crawlee-export"])
    assert [(f.rule_id, f.severity) for f in findings] == [("crawlee-export/needs-schema", "info")]
    assert "--schema" in findings[0].hint


def test_manifest_without_parquet(workdir):
    write_json(workdir.root / "items.parquet.manifest.json", {"format_version": 1})
    findings = run(only=["crawlee-export"])
    assert [f.rule_id for f in findings] == ["crawlee-export/missing-parquet"]


def test_missing_pyarrow_is_an_info_with_install_hint(workdir, monkeypatch):
    write_json(workdir.root / "items.parquet.manifest.json", {"format_version": 1})
    (workdir.root / "items.parquet").write_bytes(b"PAR1")
    monkeypatch.setattr(check_module, "available", lambda module: False)
    findings = run(only=["crawlee-export"])
    assert [(f.rule_id, f.severity) for f in findings] == [("crawlee-export/missing-dependency", "info")]
    assert "qawrench[parquet]" in findings[0].hint


def test_parquet_export_verified_and_tampered(workdir):
    pytest.importorskip("pyarrow")
    from qawrench.crawlee_export.core import export_dataset

    dataset = workdir.root / "dataset"
    for index in (1, 2):
        write_json(dataset / f"{index:09d}.json", {"title": f"item {index}"})
    schema = write_json(workdir.root / "schema.json",
                        {"version": 1, "fields": [{"name": "title", "type": "string"}]})
    out = workdir.root / "export" / "items.parquet"
    out.parent.mkdir()
    export_dataset(dataset, schema=schema, out=out)
    findings = run("export", only=["crawlee-export"])
    assert [f.rule_id for f in findings] == ["crawlee-export/verified"]
    assert "2 rows" in findings[0].message
    data = bytearray(out.read_bytes())
    data[len(data) // 2] ^= 0xFF
    out.write_bytes(bytes(data))
    findings = run("export", only=["crawlee-export"])
    assert [(f.rule_id, f.severity) for f in findings] == [("crawlee-export/verification-failed", "error")]


# --------------------------------------------------------------------------- registry

def test_crashing_check_becomes_internal_finding(demo, monkeypatch):
    def boom(source, options):
        raise KeyError("artifact content must not leak")
        yield  # pragma: no cover

    broken = registry.Check("har", registry.TESTS, "x", frozenset({"har"}), boom)
    findings = run_checks(discover(["."]), [broken, registry.BY_NAME["flaky"]], CheckOptions())
    internal = by_rule(findings, "har/internal")
    assert len(internal) == 1 and internal[0].severity == "error"
    assert "KeyError" in internal[0].message and "leak" not in internal[0].message
    assert by_rule(findings, "flaky/flaky"), "the other checks still ran"


def test_debug_env_reraises(demo, monkeypatch):
    def boom(source, options):
        raise KeyError("x")
        yield  # pragma: no cover

    monkeypatch.setenv(registry.DEBUG_ENV, "1")
    broken = registry.Check("har", registry.TESTS, "x", frozenset({"har"}), boom)
    with pytest.raises(KeyError):
        run_checks(discover(["."]), [broken], CheckOptions())


def test_missing_declared_dependency_becomes_info(demo):
    def needs_pyarrow(source, options):
        raise ModuleNotFoundError("No module named 'pyarrow'", name="pyarrow")
        yield  # pragma: no cover

    check = registry.Check("trace-budget", registry.TESTS, "x", frozenset({"trace"}), needs_pyarrow,
                           requires=("pyarrow",), extra="parquet")
    findings = run_checks(discover(["."]), [check], CheckOptions())
    assert [(f.rule_id, f.severity) for f in findings] == [("trace-budget/missing-dependency", "info")]


def test_config_only_checks_explain_themselves_when_asked(demo):
    assert run_checks(discover(["."]), select(["page-diff"]), CheckOptions()) == []  # not requested: silent
    findings = run_checks(discover(["."]), select(["page-diff", "frontier"]), CheckOptions(),
                          requested=["page-diff", "frontier"])
    assert sorted(f.rule_id for f in findings) == ["frontier/needs-config", "page-diff/needs-config"]
    assert all(f.file is None and f.severity == "info" for f in findings)


def test_every_registered_rule_is_namespaced():
    for item in registry.CHECKS:
        assert item.area in registry.AREAS
        assert item.run is not None or item.needs_config or item.name == "parse"


def test_findings_are_frozen():
    finding = Finding("flaky", "flaky", "warning", "m")
    with pytest.raises(Exception):
        finding.message = "changed"
    with pytest.raises(ValueError):
        Finding("flaky", "flaky", "fatal", "m")
