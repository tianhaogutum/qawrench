"""Artifact discovery: by content, not by name; read-only; duplicates folded."""
import os
import shutil

import pytest

from qawrench.check.discover import DiscoveryError, classify, discover

from .conftest import (DEMO, STATEWATCH_EXAMPLE, demo_json, make_trace, playwright_report, playwright_test,
                       write_json)


def kinds(discovery):
    return {source.display: source.kind for source in discovery.sources}


def test_demo_folder_kinds(demo):
    found = discover(["."])
    assert kinds(found) == {
        "a11y/axe-home.json": "axe",
        "lighthouse/lhr-1.json": "lhr",
        "lighthouse/lhr-2.json": "lhr",
        "lighthouse/lhr-3.json": "lhr",
        "lychee.json": "lychee",
        "recordings/api.har": "har",
        "test-results/checkout-applies-a-coupon-chromium-retry1/trace.zip": "trace",
        "test-results/results.json": "playwright-report",
    }
    assert found.findings == []
    assert found.scanned == 8


@pytest.mark.parametrize("relative, kind", [
    ("test-results/results.json", "playwright-report"),
    ("recordings/api.har", "har"),
    ("a11y/axe-home.json", "axe"),
    ("lighthouse/lhr-2.json", "lhr"),
    ("lychee.json", "lychee"),
])
def test_classify_demo_documents(relative, kind):
    assert classify(demo_json(relative), relative) == kind


def test_classify_other_kinds():
    axe = demo_json("a11y/axe-home.json")
    assert classify([axe, axe]) == "axe"
    assert classify({"kind": "a11y-baseline", "findings": []}) == "a11y-baseline"
    assert classify({"total": 1, "passes": 0, "errors": 1, "results": {}}) == "pa11y-ci"
    assert classify({"results": {"tool": {"name": "playwright"}, "tests": []}}) == "ctrf"
    assert classify({"format_version": 1}, "data.parquet.manifest.json") == "parquet-export"
    assert classify({"format_version": 1}, "data.json") is None
    for value in ({"name": "pkg", "version": "1.0.0"}, [], [1, 2], "text", None, {"log": {}}):
        assert classify(value) is None


def test_classify_statewatch_report():
    import json

    report = json.loads(STATEWATCH_EXAMPLE.read_text(encoding="utf-8"))
    assert classify(report, "report.json") == "statewatch-report"
    # The run manifest next to it, and other schema-version-1 documents, are not reports.
    assert classify({"schema_version": 1, "run_id": "x", "policy": {}}, "run.json") is None
    assert classify({"schema_version": 1, "tests": []}, "report.json") is None
    assert classify({**report, "schema_version": 2}, "report.json") is None


def test_statewatch_default_output_and_replay_children(workdir):
    import json

    report = json.loads(STATEWATCH_EXAMPLE.read_text(encoding="utf-8"))
    output = workdir.root / ".statewatch"
    write_json(output / "report.json", report)
    write_json(output / "run.json", {"schema_version": 1, "run_id": report["run_id"]})
    child = write_json(output / "replays" / report["run_id"] / "001" / "report.json", report)
    assert kinds(discover(["."])) == {os.path.join(".statewatch", "report.json"): "statewatch-report"}
    # A child report named explicitly is still read.
    explicit = os.path.relpath(child, workdir.root)
    assert kinds(discover([explicit])) == {explicit: "statewatch-report"}


def test_non_artifacts_are_skipped_silently(workdir):
    workdir("package.json", "not-junit.xml")
    write_json(workdir.root / "tsconfig.json", {"compilerOptions": {}})
    (workdir.root / "notes.txt").write_text("not json")
    found = discover(["."])
    assert found.sources == [] and found.findings == []


def test_broken_report_is_reported_only_when_its_name_says_report(workdir):
    workdir("broken-results.json")
    (workdir.root / "random.json").write_text("{ nope")
    found = discover(["."])
    assert [(f.rule_id, f.file, f.severity) for f in found.findings] == [
        ("parse/invalid-json", "broken-results.json", "warning")]
    assert "line" in found.findings[0].message


def test_explicit_unrecognised_file_gets_info(workdir):
    workdir("package.json")
    found = discover(["package.json"])
    assert [(f.rule_id, f.severity) for f in found.findings] == [("parse/unrecognised", "info")]


def test_junit_and_ctrf_detected(workdir):
    workdir("junit.xml", "ctrf.json", "pa11y-ci.json", "attached.har")
    assert kinds(discover(["."])) == {"junit.xml": "junit", "ctrf.json": "ctrf", "pa11y-ci.json": "pa11y-ci",
                                      "attached.har": "har"}


def test_identical_trace_copies_are_folded(workdir):
    original = make_trace(workdir.root / "test-results" / "t-chromium" / "trace.zip")
    copy = workdir.root / "playwright-report" / "data" / ("a" * 40 + ".zip")
    copy.parent.mkdir(parents=True)
    shutil.copy(original, copy)
    found = discover(["."])
    traces = [source for source in found.sources if source.kind == "trace"]
    assert [(s.display, s.copies) for s in traces] == [("test-results/t-chromium/trace.zip", 1)]


def test_zip_without_trace_stream_is_not_a_trace(workdir):
    import zipfile

    with zipfile.ZipFile(workdir.root / "bundle.zip", "w") as archive:
        archive.writestr("index.html", "<html></html>")
    assert discover(["."]).sources == []


def test_lighthouseci_dot_directory_is_scanned_other_dot_dirs_are_not(workdir):
    lhr = demo_json("lighthouse/lhr-1.json")
    write_json(workdir.root / ".lighthouseci" / "lhr-1.json", lhr)
    write_json(workdir.root / ".cache" / "lhr-1.json", lhr)
    write_json(workdir.root / "node_modules" / "x" / "lhr-1.json", lhr)
    assert kinds(discover(["."])) == {os.path.join(".lighthouseci", "lhr-1.json"): "lhr"}


def test_crawlee_dataset_directory(workdir):
    dataset = workdir.root / "storage" / "datasets" / "default"
    for index in (1, 2, 3):
        write_json(dataset / f"{index:09d}.json", {"url": f"https://site.example.test/{index}"})
    write_json(dataset / "__metadata__.json", {"id": "default"})
    found = discover(["."])
    assert kinds(found) == {os.path.join("storage", "datasets", "default"): "crawlee-dataset"}


def test_playwright_report_with_any_name(workdir):
    write_json(workdir.root / "out" / "pw.json", playwright_report(playwright_test("ok", ["passed"])))
    assert kinds(discover(["out"])) == {os.path.join("out", "pw.json"): "playwright-report"}


def test_bad_arguments():
    with pytest.raises(DiscoveryError, match="no such file"):
        discover(["does-not-exist"])
    with pytest.raises(DiscoveryError, match="empty path"):
        discover([""])


def test_discovery_writes_nothing(demo):
    before = sorted(str(p) for p in demo.rglob("*"))
    discover(["."])
    assert sorted(str(p) for p in demo.rglob("*")) == before
    assert DEMO.is_dir()
