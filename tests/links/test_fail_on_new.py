"""CLI gate contract: valid baseline required; only new results fail CI.

Existing before/after fixtures distinguish unchanged, moved, and new repeated
uses of one URL. Gate decisions never filter SARIF or bypass validation/writes.
"""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from qawrench.links import convert, load_json


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/links/fixtures/baseline"


class FailOnNewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "page.md"
        self.source.write_bytes((FIXTURE / "before.md").read_bytes())
        self.report = load_json(FIXTURE / "lychee.json")
        self.manifest = load_json(FIXTURE / "before.json")
        self.baseline = self.root / "baseline.sarif"
        self.baseline.write_text(json.dumps(convert(self.report, root=self.root,
                                                  manifest=self.manifest)), encoding="utf-8")
        self.baseline_bytes = self.baseline.read_bytes()
        self.report_path = self.root / "lychee.json"
        self.manifest_path = self.root / "occurrences.json"
        self.output = self.root / "output.sarif"

    def cli(self, *args, baseline=True):
        self.report_path.write_text(json.dumps(self.report), encoding="utf-8")
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        command = [sys.executable, "-m", "qawrench.links", str(self.report_path),
                   "--root", str(self.root), "--manifest", str(self.manifest_path), "--fail-on-new"]
        if baseline:
            command += ["--baseline", str(self.baseline)]
        return subprocess.run([*command, *map(str, args)], cwd=ROOT,
                              capture_output=True, text=True, timeout=15)

    def after(self):
        self.source.write_bytes((FIXTURE / "after.md").read_bytes())
        self.manifest = load_json(FIXTURE / "after.json")

    def assert_invalid_preserves_output(self, *args, message, **kwargs):
        self.output.write_bytes(b"keep existing output\n")
        result = self.cli("--out", self.output, *args, **kwargs)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(message, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.output.read_bytes(), b"keep existing output\n")
        return result

    def test_new_repeated_occurrence_fails_after_complete_stdout_or_file(self):
        self.after()
        expected = {"new": 1, "unchanged": 0, "updated": 1, "moved": 1, "absent": 1}
        for file_output in (False, True):
            with self.subTest(file_output=file_output):
                result = self.cli(*(["--out", self.output] if file_output else []))
                self.assertEqual(result.returncode, 1, result.stderr)
                summary = json.loads(result.stderr)
                self.assertEqual(summary["baselineComparison"], expected)
                sarif = json.loads(self.output.read_text() if file_output else result.stdout)
                run = sarif["runs"][0]
                self.assertEqual(run["properties"]["summary"], summary)
                self.assertEqual([r["baselineState"] for r in run["results"]], ["updated", "new"])
                self.assertEqual(run["results"][1]["properties"]["occurrenceId"], "new-guide")
                if file_output:
                    self.assertEqual(result.stdout, "")
        self.assertEqual(self.baseline.read_bytes(), self.baseline_bytes)

    def test_unchanged_moved_updated_and_absent_only_pass(self):
        original_report, original_manifest = deepcopy(self.report), deepcopy(self.manifest)
        for case, expected in (
            ("unchanged", {"new": 0, "unchanged": 2, "updated": 0, "moved": 0, "absent": 0}),
            ("moved", {"new": 0, "unchanged": 0, "updated": 1, "moved": 1, "absent": 1}),
            ("updated", {"new": 0, "unchanged": 0, "updated": 2, "moved": 0, "absent": 0}),
            ("absent", {"new": 0, "unchanged": 0, "updated": 0, "moved": 0, "absent": 2}),
        ):
            with self.subTest(case=case):
                self.source.write_bytes((FIXTURE / "before.md").read_bytes())
                self.report, self.manifest = deepcopy(original_report), deepcopy(original_manifest)
                if case == "moved":
                    self.after()
                    self.manifest = self.manifest[:1]
                elif case == "updated":
                    self.report["error_map"]["page.md"][0]["status"]["code"] = 500
                elif case == "absent":
                    self.report.update(total=0, errors=0, error_map={})
                    self.manifest = []
                result = self.cli("--out", self.output)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stderr)["baselineComparison"], expected)
                results = json.loads(self.output.read_text())["runs"][0]["results"]
                self.assertEqual(len(results), expected["unchanged"] + expected["updated"])
                if case == "moved":
                    self.assertEqual(results[0]["properties"]["baselineMatch"], "moved")
                self.assertEqual(self.baseline.read_bytes(), self.baseline_bytes)

    def test_dry_run_uses_gate_without_writes(self):
        for new in (False, True):
            if new:
                self.after()
            for existing in (False, True):
                with self.subTest(new=new, existing=existing):
                    self.output.unlink(missing_ok=True)
                    if existing:
                        self.output.write_bytes(b"keep")
                    result = self.cli("--out", self.output, "--dry-run")
                    self.assertEqual(result.returncode, int(new), result.stderr)
                    self.assertEqual(json.loads(result.stderr)["baselineComparison"]["new"], int(new))
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(self.output.exists(), existing)
                    if existing:
                        self.assertEqual(self.output.read_bytes(), b"keep")
        self.assertEqual(self.baseline.read_bytes(), self.baseline_bytes)

    def test_baseline_required_and_failure_gates_mutually_exclusive(self):
        for dry in ([], ["--dry-run"]):
            with self.subTest(dry=dry):
                self.assert_invalid_preserves_output(*dry, baseline=False,
                                                     message="--fail-on-new requires --baseline")
        self.assert_invalid_preserves_output("--fail-on-findings", message="not allowed with argument")

    def test_valid_empty_baseline_counts_occurrences_and_allows_clean_report(self):
        clean = {**self.report, "total": 0, "errors": 0, "error_map": {}}
        self.baseline.write_text(json.dumps(convert(clean, root=self.root)), encoding="utf-8")
        result = self.cli()
        self.assertEqual(result.returncode, 1, result.stderr)
        summary = json.loads(result.stderr)
        self.assertEqual(summary["inputFindings"], 1)
        self.assertEqual(summary["baselineComparison"]["new"], 2)  # Two uses, one URL.
        self.report, self.manifest = clean, []
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stderr)["baselineComparison"],
                         {"new": 0, "unchanged": 0, "updated": 0, "moved": 0, "absent": 0})
        self.assertEqual(json.loads(result.stdout)["runs"][0]["results"], [])

    def test_invalid_baselines_fail_closed_including_dry_run(self):
        original = json.loads(self.baseline_bytes)
        wrong_root = deepcopy(original)
        wrong_root["runs"][0]["originalUriBaseIds"]["%SRCROOT%"]["uri"] = "file:///different/root/"
        forged = deepcopy(original)
        forged["runs"][0]["results"][0]["partialFingerprints"]["lycheeStableOccurrence/v1"] = "forged"
        cases = [(None, "cannot read"), ("{broken", "cannot read JSON"), ("null", "baseline"),
                 (json.dumps(wrong_root), "source-root"), (json.dumps(forged), "canonical")]
        for content, message in cases:
            for dry in ([], ["--dry-run"]):
                with self.subTest(content=content, dry=dry):
                    self.baseline.unlink(missing_ok=True)
                    if content is not None:
                        self.baseline.write_text(content, encoding="utf-8")
                    self.assert_invalid_preserves_output(*dry, message=message)

    def test_invalid_current_report_and_evidence_fail_before_gate(self):
        original_report = deepcopy(self.report)
        self.report = {}
        self.assert_invalid_preserves_output(message="report")
        self.report = original_report
        self.manifest[0]["line"] = 1
        self.assert_invalid_preserves_output(message="URL mismatch")

    def test_output_failure_and_baseline_overwrite_return_two(self):
        for new in (False, True):
            with self.subTest(new=new):
                if new:
                    self.after()
                result = self.cli("--out", self.root / "missing" / "report.sarif")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("No such file", result.stderr)
                self.assertEqual(result.stdout, "")
                result = self.cli("--out", self.baseline)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("would overwrite", result.stderr)
                self.assertEqual(self.baseline.read_bytes(), self.baseline_bytes)

    def test_unidentified_move_is_new_without_lineage_guess(self):
        for item in self.manifest:
            del item["occurrenceId"]
        self.baseline.write_text(json.dumps(convert(self.report, root=self.root,
                                                  manifest=self.manifest)), encoding="utf-8")
        self.source.write_text("\n" * 5 + self.source.read_text(), encoding="utf-8")
        for item in self.manifest:
            item["line"] += 5
        result = self.cli()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stderr)["baselineComparison"],
                         {"new": 2, "unchanged": 0, "updated": 0, "moved": 0, "absent": 2})


if __name__ == "__main__":
    unittest.main()
