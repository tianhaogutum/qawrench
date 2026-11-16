"""Offline baseline comparison with caller-owned occurrence identities."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from qawrench.links import InputError, convert, load_json


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/links/fixtures/baseline"


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report = load_json(FIXTURE / "lychee.json")
        self.before = load_json(FIXTURE / "before.json")
        self.after = load_json(FIXTURE / "after.json")
        self.snapshot("before")

    def snapshot(self, name):
        (self.root / "page.md").write_bytes((FIXTURE / (name + ".md")).read_bytes())

    def baseline(self):
        return convert(self.report, root=self.root, manifest=self.before)

    def test_move_new_repeat_and_absent_are_distinct(self):
        baseline = self.baseline()
        original = deepcopy(baseline)
        self.snapshot("after")
        run = convert(self.report, root=self.root, manifest=self.after, baseline=baseline)["runs"][0]
        moved, new = run["results"]
        self.assertEqual(moved["baselineState"], "updated")
        self.assertEqual(moved["properties"]["baselineMatch"], "moved")
        self.assertEqual(moved["properties"]["baselinePreviousRegion"],
                         {"startLine": 3, "startColumn": 1, "endLine": 3, "endColumn": 31})
        self.assertEqual(new["baselineState"], "new")
        self.assertEqual(new["properties"]["occurrenceId"], "new-guide")
        self.assertEqual(run["properties"]["summary"]["baselineComparison"],
                         {"new": 1, "unchanged": 0, "updated": 1, "moved": 1, "absent": 1})
        self.assertEqual(baseline, original)  # Inputs remain immutable.
        self.assertEqual(moved["partialFingerprints"]["lycheeStableOccurrence/v1"],
                         baseline["runs"][0]["results"][0]["partialFingerprints"]["lycheeStableOccurrence/v1"])
        self.assertNotEqual(moved["partialFingerprints"]["lycheeSourceOccurrence/v1"],
                            baseline["runs"][0]["results"][0]["partialFingerprints"]["lycheeSourceOccurrence/v1"])

    def test_unchanged_diagnostics_updated_and_chained_baseline(self):
        baseline = self.baseline()
        same = convert(self.report, root=self.root, manifest=list(reversed(self.before)), baseline=baseline)
        self.assertEqual([r["baselineState"] for r in same["runs"][0]["results"]], ["unchanged"] * 2)
        self.report["error_map"]["page.md"][0]["status"]["code"] = 500
        changed = convert(self.report, root=self.root, manifest=self.before, baseline=same)
        self.assertEqual([r["baselineState"] for r in changed["runs"][0]["results"]], ["updated"] * 2)
        self.assertEqual(changed["runs"][0]["properties"]["summary"]["baselineComparison"]["moved"], 0)
        again = convert(self.report, root=self.root, manifest=self.before, baseline=changed)
        self.assertEqual([r["baselineState"] for r in again["runs"][0]["results"]], ["unchanged"] * 2)

    def test_id_does_not_cross_source_or_url(self):
        baseline = self.baseline()
        other_url = "https://example.invalid/another"
        (self.root / "page.md").write_text(other_url + "\n", encoding="utf-8")
        self.report["error_map"]["page.md"][0]["url"] = other_url
        manifest = [{**self.before[0], "url": other_url, "line": 1}]
        run = convert(self.report, root=self.root, manifest=manifest, baseline=baseline)["runs"][0]
        self.assertEqual(run["results"][0]["baselineState"], "new")
        self.report["error_map"]["other.md"] = self.report["error_map"].pop("page.md")
        original_url = self.before[0]["url"]
        self.report["error_map"]["other.md"][0]["url"] = original_url
        (self.root / "other.md").write_text(original_url, encoding="utf-8")
        manifest[0].update(source="other.md", url=original_url)
        run = convert(self.report, root=self.root, manifest=manifest, baseline=baseline)["runs"][0]
        self.assertEqual(run["results"][0]["baselineState"], "new")

    def test_changed_failure_kind_keeps_stable_occurrence_identity(self):
        baseline = self.baseline()
        self.report.update(errors=0, timeouts=1)
        self.report["error_map"]["page.md"][0]["status"] = {"text": "Timeout"}
        run = convert(self.report, root=self.root, manifest=self.before, baseline=baseline)["runs"][0]
        self.assertEqual([r["baselineState"] for r in run["results"]], ["updated"] * 2)
        self.assertEqual(run["properties"]["summary"]["baselineComparison"]["new"], 0)

    def test_legacy_baseline_and_strict_identity_transition(self):
        # A previous round's reports have no occurrenceId/stable fingerprint.
        manifest = [{k: v for k, v in item.items() if k != "occurrenceId"} for item in self.before]
        legacy = convert(self.report, root=self.root, manifest=manifest)
        same = convert(self.report, root=self.root, manifest=manifest, baseline=legacy)["runs"][0]
        self.assertEqual(same["properties"]["summary"]["baselineComparison"]["unchanged"], 2)
        identified = convert(self.report, root=self.root, manifest=self.before, baseline=legacy)["runs"][0]
        # No fallback from a caller-owned ID to a coincident location.
        self.assertEqual(identified["properties"]["summary"]["baselineComparison"]["new"], 2)
        self.assertEqual(identified["properties"]["summary"]["baselineComparison"]["absent"], 2)

    def test_absent_historical_file_is_not_opened_or_emitted(self):
        baseline = self.baseline()
        (self.root / "page.md").unlink()
        clean = deepcopy(self.report)
        clean.update(total=0, errors=0, error_map={})
        run = convert(clean, root=self.root, baseline=baseline)["runs"][0]
        self.assertEqual(run["results"], [])
        self.assertEqual(run["properties"]["summary"]["baselineComparison"]["absent"], 2)

    def test_without_ids_only_exact_regions_match_including_markdown(self):
        manifest = [{k: v for k, v in item.items() if k != "occurrenceId"} for item in self.before]
        for markdown in (False, True):
            with self.subTest(markdown=markdown):
                self.snapshot("before")
                if markdown:
                    (self.root / "page.md").write_text("[x](https://example.invalid/broken)\n", encoding="utf-8")
                args = {"markdown": True} if markdown else {"manifest": manifest}
                baseline = convert(self.report, root=self.root, **args)
                same = convert(self.report, root=self.root, baseline=baseline, **args)
                self.assertTrue(all(r["baselineState"] == "unchanged" for r in same["runs"][0]["results"]))
                path = self.root / "page.md"
                path.write_text("\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
                moved_args = {"markdown": True} if markdown else {"manifest": [{**m, "line": m["line"] + 1} for m in manifest]}
                moved = convert(self.report, root=self.root, baseline=baseline, **moved_args)
                # One overlapping exact region can remain unchanged; no move inferred.
                self.assertEqual(moved["runs"][0]["properties"]["summary"]["baselineComparison"]["moved"], 0)

    def test_unmapped_and_aggregate_keep_honest_accounting(self):
        self.report["total"] += 3
        self.report["unsupported"] = 3
        baseline = convert(self.report, root=self.root)
        same = convert(self.report, root=self.root, baseline=baseline)["runs"][0]
        self.assertEqual([r["baselineState"] for r in same["results"]], ["unchanged"] * 2)
        self.report["unsupported"] = 2
        self.report["total"] -= 1
        changed = convert(self.report, root=self.root, baseline=baseline)["runs"][0]
        self.assertEqual(changed["results"][-1]["baselineState"], "updated")
        clean = deepcopy(self.report)
        clean.update(total=0, errors=0, unsupported=0, error_map={})
        run = convert(clean, root=self.root, baseline=baseline)["runs"][0]
        self.assertEqual(run["results"], [])
        self.assertEqual(run["properties"]["summary"]["baselineComparison"]["absent"], 2)

    def test_invalid_or_duplicate_occurrence_ids_rejected(self):
        for value in ("", None, 1, True, "bad\nkey", "\ud800"):
            with self.subTest(value=value), self.assertRaises(InputError):
                convert(self.report, root=self.root, manifest=[{**self.before[0], "occurrenceId": value}])
        with self.assertRaisesRegex(InputError, "duplicate.*occurrenceId"):
            convert(self.report, root=self.root, manifest=[self.before[0], {**self.before[1], "occurrenceId": "install-guide"}])

    def test_mismatched_root_and_mapping_mode_rejected(self):
        baseline = self.baseline()
        replica = self.root / "replica"
        replica.mkdir()
        (replica / "page.md").write_bytes((self.root / "page.md").read_bytes())
        with self.assertRaisesRegex(InputError, "source-root"):
            convert(self.report, root=replica, manifest=self.before, baseline=baseline)
        with self.assertRaisesRegex(InputError, "mapping mode"):
            convert(self.report, root=self.root, markdown=True, baseline=baseline)

    def test_malformed_baselines_fail_closed(self):
        baseline = self.baseline()
        bad_cases = [None, [], {}, {**baseline, "version": "1.0"}, {**baseline, "runs": []},
                     {**baseline, "runs": baseline["runs"] * 2}]
        for mutate in (
            lambda r: r["tool"]["driver"].update(name="other-tool"),
            lambda r: r.update(results={}),
            lambda r: r["results"].append(deepcopy(r["results"][0])),
            lambda r: r["results"][0]["partialFingerprints"].update({"lycheeSourceOccurrence/v1": "forged"}),
            lambda r: r["results"][0]["properties"].update(source="../escape.md"),
            lambda r: r["results"][0]["properties"].update(mapped=False),
            lambda r: r["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"].update(uri="other.md"),
            lambda r: r["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"].update(uriBaseId="OTHER"),
            lambda r: r["results"][0]["locations"][0]["physicalLocation"]["region"].update(startLine=True),
            lambda r: r["results"][0]["properties"].update(occurrenceId=""),
        ):
            bad = deepcopy(baseline)
            mutate(bad["runs"][0])
            bad_cases.append(bad)
        for bad in bad_cases:
            with self.subTest(baseline=bad), self.assertRaisesRegex(InputError, "baseline"):
                convert(self.report, root=self.root, manifest=self.before, baseline=bad)

    def test_cli_validation_dry_run_and_input_protection(self):
        baseline = self.root / "baseline.sarif"
        baseline.write_text(json.dumps(self.baseline()), encoding="utf-8")
        report = self.root / "lychee.json"
        report.write_text(json.dumps(self.report), encoding="utf-8")
        output = self.root / "output.sarif"
        self.snapshot("after")
        command = [sys.executable, "-m", "qawrench.links", str(report), "--root", str(self.root),
                   "--manifest", str(FIXTURE / "after.json"), "--baseline", str(baseline)]

        def cli(*args):
            return subprocess.run([*command, *map(str, args)], cwd=ROOT, capture_output=True, text=True, timeout=15)

        result = cli("--out", output, "--fail-on-findings")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stderr)["baselineComparison"]["moved"], 1)
        before = output.read_bytes()
        result = cli("--out", output, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(output.read_bytes(), before)
        before_baseline = baseline.read_bytes()
        result = cli("--out", baseline)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(baseline.read_bytes(), before_baseline)
        invalid = json.loads(before_baseline)
        invalid["runs"][0]["originalUriBaseIds"]["%SRCROOT%"]["uri"] = "file:///another/root/"
        baseline.write_text(json.dumps(invalid), encoding="utf-8")
        result = cli("--out", output)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("source-root", result.stderr)
        self.assertEqual(output.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
