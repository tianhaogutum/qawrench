"""Substantive offline format, location, accounting, and CLI regression tests."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from qawrench.links import InputError, convert, load_json, normalize_status
from qawrench.links.cli import _write_atomic


ROOT = Path(__file__).resolve().parents[2]


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "docs").mkdir()
        (self.root / "docs/page.md").write_bytes((ROOT / "examples/links/docs/page.md").read_bytes())
        self.report = load_json(ROOT / "examples/links/lychee.json")
        self.manifest = load_json(ROOT / "examples/links/occurrences.json")

    def convert(self, **kwargs):
        return convert(self.report, root=self.root, **kwargs)["runs"][0]

    def assert_bad(self, pattern, **kwargs):
        with self.assertRaisesRegex(InputError, pattern):
            self.convert(**kwargs)

    def test_example_exact_regions_and_all_errors_accounted(self):
        run = self.convert(manifest=self.manifest)
        results = run["results"]
        self.assertEqual(run["columnKind"], "unicodeCodePoints")
        self.assertEqual(len(results), 4)
        self.assertEqual([r["properties"]["category"] for r in results],
                         ["http-failure", "http-failure", "transport-error", "timeout"])
        for result, line, column in zip(results[:2], (3, 4), (16, 22)):
            physical = result["locations"][0]["physicalLocation"]
            self.assertEqual(physical["artifactLocation"]["uri"], "docs/page.md")
            self.assertEqual(physical["region"], {"startLine": line, "startColumn": column,
                                                "endLine": line, "endColumn": column + 30})
        for result in results[2:]:
            physical = result["locations"][0]["physicalLocation"]
            self.assertNotIn("region", physical)  # No fake line 1.
            self.assertFalse(result["properties"]["mapped"])
        self.assertEqual(results[2]["properties"]["originalEntry"], self.report["error_map"]["docs/page.md"][1])
        self.assertIn("Synthetic DNS failure", results[2]["message"]["text"])
        self.assertEqual(run["properties"]["summary"], {
            "inputErrors": 2, "inputTimeouts": 1, "inputFindings": 3,
            "mappedFindings": 1, "mappedResults": 2, "unmappedFindings": 2,
            "ignoredSuccess": 1, "excluded": 0, "unsupportedChecks": 0,
            "aggregateResults": 0, "resultCount": 4,
        })

    def test_historical_issue_json_end_to_end(self):
        report = load_json(ROOT / "tests/links/fixtures/issue1637.json")
        url = "https://broken1.myname.nl/"
        (self.root / "mysql-compatibility.md").write_text(
            f"# Synthetic test source\n😀 {url} and {url}\n", encoding="utf-8")
        manifest = [{"source": "mysql-compatibility.md", "url": url, "line": 2, "column": c}
                    for c in (3, 3 + len(url) + 5)]
        run = convert(report, root=self.root, manifest=manifest)["runs"][0]
        self.assertEqual(len(run["results"]), 3)
        self.assertEqual(run["properties"]["summary"]["ignoredSuccess"], 36)
        self.assertEqual(run["properties"]["summary"]["excluded"], 14)
        self.assertEqual(run["properties"]["summary"]["unmappedFindings"], 1)
        self.assertTrue(all(r["ruleId"] == "LYCHEE-TRANSPORT" for r in run["results"]))

    def test_sarif_structure_rules_and_unique_stable_fingerprints(self):
        original = deepcopy(self.report)
        sarif = convert(self.report, root=self.root, manifest=self.manifest)
        self.assertEqual(sarif["version"], "2.1.0")
        self.assertTrue(sarif["$schema"].startswith("https://"))
        run = sarif["runs"][0]
        rules = run["tool"]["driver"]["rules"]
        self.assertEqual(len({r["id"] for r in rules}), 4)
        fingerprints = []
        for result in run["results"]:
            self.assertEqual(result["ruleId"], rules[result["ruleIndex"]]["id"])
            self.assertTrue(result["message"]["text"])
            fingerprint = result["partialFingerprints"]["lycheeSourceOccurrence/v1"]
            self.assertRegex(fingerprint, r"^[0-9a-f]{64}$")
            fingerprints.append(fingerprint)
        self.assertEqual(len(set(fingerprints)), 4)
        self.report["error_map"]["docs/page.md"].reverse()
        self.manifest.reverse()
        self.assertEqual(sarif, convert(self.report, root=self.root, manifest=self.manifest))
        self.report = original
        self.assertEqual(self.convert(manifest=self.manifest), run)
        self.report["error_map"]["docs/page.md"][1]["status"]["details"] = "Changed diagnostic text"
        self.assertEqual([r["partialFingerprints"] for r in self.convert(manifest=self.manifest)["results"]],
                         [r["partialFingerprints"] for r in run["results"]])

    def test_no_manifest_keeps_every_error_at_file_level(self):
        run = self.convert()
        self.assertEqual(run["properties"]["summary"]["unmappedFindings"], 3)
        self.assertEqual(len(run["results"]), 3)
        self.assertTrue(all("region" not in r["locations"][0]["physicalLocation"] for r in run["results"]))

    def test_fingerprints_ignore_absolute_root_but_locations_declare_it(self):
        replica = self.root / "replica"
        (replica / "docs").mkdir(parents=True)
        (replica / "docs/page.md").write_bytes((self.root / "docs/page.md").read_bytes())
        original = self.convert(manifest=self.manifest)
        relocated = convert(self.report, root=replica, manifest=self.manifest)["runs"][0]
        self.assertEqual([r["partialFingerprints"] for r in original["results"]],
                         [r["partialFingerprints"] for r in relocated["results"]])
        self.assertEqual(relocated["originalUriBaseIds"]["%SRCROOT%"]["uri"], replica.as_uri() + "/")
        self.assertEqual(relocated["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uriBaseId"], "%SRCROOT%")

    def test_unicode_columns_are_code_points_and_crlf_is_supported(self):
        source = self.root / "docs/page.md"
        source.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))
        self.convert(manifest=self.manifest)
        self.manifest[0]["column"] += 1  # UTF-16 counts the emoji as two units.
        self.assert_bad("URL mismatch.*Unicode code-point", manifest=self.manifest)

    def test_percent_encoded_artifact_uri_and_exact_url(self):
        name = "docs/café #100%.md"
        (self.root / name).write_bytes((self.root / "docs/page.md").read_bytes())
        self.report["error_map"][name] = self.report["error_map"].pop("docs/page.md")
        for o in self.manifest:
            o["source"] = name
        uri = self.convert(manifest=self.manifest)["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        self.assertEqual(uri, "docs/caf%C3%A9%20%23100%25.md")
        self.manifest[0]["url"] += "/"
        self.assert_bad("absent from error_map", manifest=self.manifest)

    def test_manifest_does_not_cross_map_sources(self):
        (self.root / "other.md").write_text("https://example.invalid/broken", encoding="utf-8")
        entry = deepcopy(self.report["error_map"]["docs/page.md"][0])
        self.report["error_map"]["other.md"] = [entry]
        self.report["errors"] += 1
        self.report["total"] += 1
        run = self.convert(manifest=self.manifest)
        self.assertEqual(run["properties"]["summary"]["unmappedFindings"], 3)
        other = [r for r in run["results"] if r["properties"]["source"] == "other.md"]
        self.assertEqual(len(other), 1)
        self.assertNotIn("region", other[0]["locations"][0]["physicalLocation"])

    def test_detailed_stats_and_nonstandard_accepted_success(self):
        self.report["detailed_stats"] = True
        # Upstream --accept can classify 404 as successful.
        self.report["success_map"] = {"docs/page.md": [{"url": "https://example.invalid/ok", "status": {"text": "404 Not Found", "code": 404}}]}
        self.report["excludes"] = 1
        self.report["total"] += 1
        self.report["excluded_map"] = {"docs/page.md": [{"url": "https://excluded.invalid/", "status": {"text": ""}}]}
        run = self.convert()
        self.assertEqual(run["properties"]["summary"]["ignoredSuccess"], 1)
        self.assertEqual(len(run["results"]), 3)
        self.report["success_map"]["docs/page.md"][0]["status"] = {"text": "Timeout"}
        self.assert_bad("expected success status")

    def test_unsupported_counter_is_explicit_unlocated_aggregate(self):
        self.report["unsupported"] = 7
        self.report["total"] += 7
        run = self.convert()
        result = run["results"][-1]
        self.assertEqual(result["ruleId"], "LYCHEE-UNSUPPORTED")
        self.assertEqual(result["properties"]["upstreamCount"], 7)
        self.assertTrue(result["properties"]["summaryOnly"])
        self.assertNotIn("locations", result)
        self.assertNotIn("originalEntry", result["properties"])
        self.assertEqual(run["properties"]["summary"]["aggregateResults"], 1)

    def test_empty_report_has_no_results(self):
        for name in ("total", "successful", "errors", "timeouts"):
            self.report[name] = 0
        self.report["error_map"] = {}
        self.assertEqual(self.convert()["results"], [])

    def test_normalize_documented_statuses(self):
        cases = [({"text": "Network error", "code": 500}, "http-failure"),
                 ({"text": "Network error", "details": "DNS failed"}, "transport-error"),
                 ({"text": "Timeout"}, "timeout"),
                 ({"text": "Timeout (408)", "code": 408}, "timeout"),
                 ({"text": "Unsupported: Unsupported URI type: 'slack'"}, "unsupported")]
        for status, expected in cases:
            with self.subTest(status=status):
                self.assertEqual(normalize_status(status), expected)

    def test_unknown_or_malformed_status_rejected(self):
        statuses = [None, "error", {}, {"code": 404}, {"text": 404},
                    {"text": "Network error", "code": True},
                    {"text": "Network error", "code": 99},
                    {"text": "Network error", "code": 1000},
                    {"text": "Network error", "code": "404"},
                    {"text": "Network error", "details": {}},
                    {"text": "Network error", "code": 404, "details": "no"},
                    {"text": "Timeout", "code": 408},
                    {"text": "Timeout", "details": "no"},
                    {"text": "Unsupported: "}, {"text": "future failure"},
                    {"text": "Network error", "span": {}},
                    {"text": "200 OK", "code": 201}]
        for status in statuses:
            with self.subTest(status=status), self.assertRaises(InputError):
                normalize_status(status)

    def test_wrong_top_level_and_unknown_entry_fields_rejected(self):
        for report in ([], {}, {**self.report, "future": 1}):
            with self.subTest(report=type(report)), self.assertRaises(InputError):
                convert(report, root=self.root)
        self.report["error_map"]["docs/page.md"][0]["span"] = {"line": 1}
        self.assert_bad("unknown keys.*span")

    def test_malformed_entries_maps_and_counters_rejected(self):
        original = deepcopy(self.report)
        for key, value in [("total", True), ("errors", -1), ("timeouts", 1.0),
                           ("duration_secs", "0"), ("detailed_stats", 0),
                           ("error_map", []), ("success_map", None)]:
            with self.subTest(key=key, value=value):
                self.report = {**original, key: value}
                self.assert_bad("expected")
        for entry in [None, {}, {"url": "", "status": {"text": "Timeout"}},
                      {"url": "x\ny", "status": {"text": "Timeout"}}]:
            self.report = deepcopy(original)
            self.report["error_map"]["docs/page.md"][0] = entry
            self.assert_bad("expected|missing keys|single-line")
        self.report = deepcopy(original)
        self.report["error_map"]["docs/page.md"] = {}
        self.assert_bad("expected an array")

    def test_count_disagreements_rejected(self):
        original = deepcopy(self.report)
        changes = [("total", 5), ("errors", 3), ("timeouts", 2), ("successful", 2)]
        for key, value in changes:
            with self.subTest(key=key):
                self.report = deepcopy(original)
                self.report[key] = value
                if key != "total":
                    self.report["total"] += value - original[key]
                # Successful counters may be absent from maps in compact mode.
                if key == "successful":
                    self.report["detailed_stats"] = True
                self.assert_bad("count disagreement")
        self.report = deepcopy(original)
        self.report["error_map"]["docs/page.md"].pop()
        self.assert_bad("timeouts: count disagreement")

    def test_cached_suggestions_redirect_and_unknown_rejected_explicitly(self):
        original = deepcopy(self.report)
        for key, value, error in [("cached", 1, "cached responses"),
                                  ("suggestion_map", {"docs/page.md": []}, "suggestions"),
                                  ("redirects", 1, "unknown/redirect"),
                                  ("unknown", 1, "unknown/redirect")]:
            with self.subTest(key=key):
                self.report = {**original, key: value}
                self.assert_bad(error)

    def test_success_in_error_map_and_unsupported_record_not_silently_dropped(self):
        for status in ({"text": "200 OK", "code": 200}, {"text": "Unsupported: scheme"}):
            self.report["error_map"]["docs/page.md"][0]["status"] = status
            self.assert_bad("does not belong")

    def test_duplicate_report_url_rejected(self):
        self.report["error_map"]["docs/page.md"].append(deepcopy(self.report["error_map"]["docs/page.md"][0]))
        self.assert_bad("duplicate URL")

    def test_bad_manifests_are_errors(self):
        for manifest in (None, {}, "x", [None], [{}], [{**self.manifest[0], "extra": True}],
                         [self.manifest[0], self.manifest[0]]):
            with self.subTest(manifest=manifest):
                self.assert_bad("manifest", manifest=manifest)
        for key, value in [("line", 0), ("column", 0), ("line", True),
                           ("column", 1.0), ("line", 999), ("column", 999),
                           ("line", 2), ("url", "https://EXAMPLE.invalid/broken")]:
            with self.subTest(key=key, value=value):
                occurrence = {**self.manifest[0], key: value}
                self.assert_bad("manifest", manifest=[occurrence])
        for source in ("../outside.md", "/etc/passwd", "docs/../page.md"):
            self.assert_bad("relative POSIX", manifest=[{**self.manifest[0], "source": source}])

    def test_malicious_source_paths_rejected_even_without_manifest(self):
        entries = self.report["error_map"]["docs/page.md"]
        for source in ("/etc/passwd", "../outside.md", "docs/../../outside", "docs/../page.md",
                       "./docs/page.md", "docs//page.md", "docs/page.md/", "C:/docs/page.md",
                       "https://host/source", "docs\\page.md", "docs/\x00.md", "docs/\n.md"):
            with self.subTest(source=source):
                self.report["error_map"] = {source: entries}
                self.assert_bad("relative POSIX")

    def test_missing_unreadable_non_utf8_and_directory_sources_rejected(self):
        source = self.root / "docs/page.md"
        source.unlink()
        self.assert_bad("cannot read source")
        source.mkdir()
        self.assert_bad("regular source file")
        source.rmdir()
        source.write_bytes(b"\xff\xfe")
        self.assert_bad("cannot read source")
        with patch.object(Path, "read_bytes", side_effect=PermissionError("synthetic permission failure")):
            self.assert_bad("permission failure")
        with self.assertRaisesRegex(InputError, "root"):
            convert(self.report, root=self.root / "missing")

    def test_symlink_escape_and_loop_rejected(self):
        with tempfile.TemporaryDirectory() as outer:
            external = Path(outer) / "external.md"
            external.write_text("https://example.invalid/broken", encoding="utf-8")
            source = self.root / "docs/page.md"
            source.unlink()
            source.symlink_to(external)
            self.assert_bad("symlink escape")
            source.unlink()
            source.symlink_to(source.name)
            self.assert_bad("cannot read source")

    def test_output_cannot_overwrite_source_or_input(self):
        source = self.root / "docs/page.md"
        self.assert_bad("would overwrite", output_path=source)
        report_path = self.root / "report.json"
        report_path.write_text(json.dumps(self.report), encoding="utf-8")
        self.assert_bad("would overwrite", output_path=report_path, protected_paths=(report_path,))
        hardlink = self.root / "alias.md"
        hardlink.hardlink_to(source)
        self.assert_bad("would overwrite", output_path=hardlink)

    def test_json_duplicate_keys_invalid_numbers_and_encoding(self):
        path = self.root / "input.json"
        for data in (b'{"error_map":{},"error_map":{}}', b'{"x":NaN}', b'{"x":Infinity}', b'{', b'\xff',
                     b'{"x":' + b'1' * 5000 + b'}'):
            path.write_bytes(data)
            with self.subTest(data=data), self.assertRaises(InputError):
                load_json(path)
        with self.assertRaises(InputError):
            load_json(self.root / "absent.json")
        self.report["error_map"]["docs/page.md"][0]["url"] = "\ud800"
        self.assert_bad("invalid Unicode")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "report.sarif"

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "qawrench.links", *map(str, args)],
                              cwd=ROOT, text=True, capture_output=True, timeout=15)

    def base(self):
        return [ROOT / "examples/links/lychee.json", "--manifest", ROOT / "examples/links/occurrences.json",
                "--root", ROOT / "examples/links"]

    def test_cli_fixture_file_stdout_summary_and_ci_exit(self):
        completed = self.cli(*self.base(), "--out", self.out)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stderr)["mappedResults"], 2)
        self.assertEqual(completed.stdout, "")
        file_data = self.out.read_text(encoding="utf-8")
        stdout_run = self.cli(*self.base(), "--fail-on-findings")
        self.assertEqual(stdout_run.returncode, 1, stdout_run.stderr)
        self.assertEqual(stdout_run.stdout, file_data)
        self.assertEqual(len(json.loads(file_data)["runs"][0]["results"]), 4)

    def test_ci_clean_report_exits_zero(self):
        report = load_json(ROOT / "examples/links/lychee.json")
        for key in ("total", "successful", "errors", "timeouts"):
            report[key] = 0
        report["error_map"] = {}
        path = self.root / "clean.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        completed = self.cli(path, "--root", self.root, "--out", self.out, "--fail-on-findings")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stderr)["resultCount"], 0)
        self.assertEqual(json.loads(self.out.read_text())["runs"][0]["results"], [])

    def test_dry_run_never_writes_even_with_out(self):
        before = {p: p.read_bytes() for p in (ROOT / "examples/links").rglob("*") if p.is_file()}
        completed = self.cli(*self.base(), "--out", self.out, "--dry-run")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertFalse(self.out.exists())
        self.out.write_text("keep", encoding="utf-8")
        completed = self.cli(*self.base(), "--out", self.out, "--dry-run")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.out.read_text(), "keep")
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_invalid_input_and_manifest_preserve_existing_output(self):
        self.out.write_text("keep", encoding="utf-8")
        bad = self.root / "bad.json"
        for content in ("{}", "null", "{broken", '{"total": 1, "total": 2}'):
            bad.write_text(content, encoding="utf-8")
            completed = self.cli(bad, "--out", self.out)
            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertEqual(self.out.read_text(), "keep")
        bad.write_text("null", encoding="utf-8")
        completed = self.cli(ROOT / "examples/links/lychee.json", "--root", ROOT / "examples/links",
                             "--manifest", bad, "--out", self.out)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("manifest", completed.stderr)
        self.assertEqual(self.out.read_text(), "keep")

    def test_output_write_failure_and_symlink_do_not_damage_files(self):
        completed = self.cli(*self.base(), "--out", self.root / "missing" / "out.sarif")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("No such file", completed.stderr)
        self.out.symlink_to(ROOT / "examples/links/docs/page.md")
        completed = self.cli(*self.base(), "--out", self.out)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("symlink outputs", completed.stderr)

    def test_atomic_write_cleanup_on_failure(self):
        self.out.write_text("keep", encoding="utf-8")
        with patch("qawrench.links.cli.os.replace", side_effect=OSError("synthetic replace failure")):
            with self.assertRaisesRegex(OSError, "replace failure"):
                _write_atomic(self.out, "new report")
        self.assertEqual(self.out.read_text(), "keep")
        self.assertEqual(list(self.root.glob(".qawrench-links-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
