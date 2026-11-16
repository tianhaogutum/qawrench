"""Enforcement contract: reject the whole candidate, retain audit and old outputs.

Every valid original, including collapsed/checkpoint members, must be allowed.
Disallow or unknown => exit 2, rejected report, no frontier/checkpoint write.
Missing enforcement inputs are fatal before any output; dry-run is write-free.
"""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qawrench.frontier import AuditError, Policy, RobotsPolicy, audit, load_requests
from qawrench.frontier.cli import atomic_write, main


class RobotsEnforcementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "requests.jsonl"
        self.manifest = self.root / "robots.json"
        self.frontier = self.root / "frontier.jsonl"
        self.report = self.root / "report.json"
        self.checkpoint = self.root / "checkpoint.json"
        self.write_requests("https://example.test/private", "https://example.test/public",
                            "http://example.test/private", "https://down.test/x")
        self.manifest.write_text(json.dumps({"version": 1, "origins": {
            "https://example.test": {"status": "available", "source": "synthetic:snapshot",
                                     "text": "User-agent: AuditBot\nDisallow: /private\n"},
            "https://down.test": {"status": "unavailable", "source": "synthetic:missing",
                                  "reason": "snapshot not captured"}}}), encoding="utf-8")
        self.options = ["--robots", str(self.manifest), "--user-agent", "AuditBot"]

    def write_requests(self, *urls):
        self.input.write_text("".join(json.dumps({"url": u}) + "\n" for u in urls), encoding="utf-8")

    def cli(self, *options):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([str(self.input), "--frontier", str(self.frontier), "--report", str(self.report),
                         *options])
        return code, out.getvalue(), err.getvalue()

    def test_rejected_run_retains_complete_audit_and_preserves_existing_outputs(self):
        self.frontier.write_bytes(b"old frontier\n")
        self.checkpoint.write_bytes(b"old checkpoint\n")
        self.report.write_text("old report")
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden")), \
                patch("qawrench.frontier.cli.atomic_write", wraps=atomic_write) as write:
            code, _, err = self.cli(*self.options, "--enforce-robots",
                                    "--checkpoint-out", str(self.checkpoint))
        self.assertEqual([call.args[0] for call in write.call_args_list], [self.report])
        self.assertEqual(code, 2)
        self.assertIn("robots enforcement", err)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["status"], "rejected")
        self.assertFalse(report["frontier_emitted"])
        self.assertFalse(report["checkpoint"]["emitted"])
        self.assertEqual(report["counts"]["accepted"], 4)  # simulated candidate, not emitted
        robots = report["robots"]
        self.assertEqual(robots["mode"], "offline-enforced")
        self.assertEqual(robots["counts"], {"allow": 1, "disallow": 1, "unknown": 2})
        origins = robots["origins"]
        self.assertEqual(origins[0]["requests"][0]["winning_rule"]["line"], 2)
        self.assertEqual(origins[0]["source"], "synthetic:snapshot")
        self.assertEqual(origins[1]["requests"][0]["reason"], "no local snapshot supplied")
        self.assertEqual(origins[2]["requests"][0]["reason"], "snapshot not captured")
        self.assertEqual(self.frontier.read_bytes(), b"old frontier\n")
        self.assertEqual(self.checkpoint.read_bytes(), b"old checkpoint\n")

    def test_each_blocking_decision_alone_prevents_creating_a_frontier_or_checkpoint(self):
        for url, decision in (("https://example.test/private", "disallow"),
                              ("http://example.test/public", "unknown"),
                              ("https://down.test/public", "unknown")):
            with self.subTest(url=url):
                self.write_requests(url)
                self.assertEqual(self.cli(*self.options, "--enforce-robots",
                                         "--checkpoint-out", str(self.checkpoint))[0], 2)
                report = json.loads(self.report.read_text())
                self.assertEqual(report["robots"]["counts"][decision], 1)
                self.assertEqual(sum(report["robots"]["counts"].values()), 1)
                self.assertFalse(self.frontier.exists())
                self.assertFalse(self.checkpoint.exists())

    def test_informational_default_still_emits_all_decisions(self):
        self.assertEqual(self.cli(*self.options)[0], 0)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["robots"]["mode"], "offline-informational")
        self.assertEqual(report["robots"]["counts"], {"allow": 1, "disallow": 1, "unknown": 2})
        self.assertTrue(report["frontier_emitted"])
        self.assertEqual(len(self.frontier.read_text().splitlines()), 4)

    def test_all_allow_emits_first_originals_with_exact_methods_bodies_and_metadata(self):
        records = [{"url": "https://EXAMPLE.test:443/public?x=%2f#keep", "source": "synthetic:first",
                    "metadata": {"label": "synthetic"}},
                   {"url": "https://example.test/public", "method": "POST", "body": "a=1 "},
                   {"url": "https://example.test/public", "method": "POST", "body": "a=1"}]
        self.input.write_text("".join(json.dumps(r) + "\n" for r in [*records, records[0]]))
        self.assertEqual(self.cli(*self.options, "--enforce-robots",
                                 "--checkpoint-out", str(self.checkpoint))[0], 0)
        frontier = [json.loads(line) for line in self.frontier.read_text().splitlines()]
        self.assertEqual(frontier, [{"method": "GET", "body": "", **records[0]}, *records[1:]])
        report = json.loads(self.report.read_text())
        self.assertEqual(report["robots"]["counts"], {"allow": 4, "disallow": 0, "unknown": 0})
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["counts"]["accepted"], 3)
        self.assertTrue(report["frontier_emitted"])
        self.assertTrue(report["checkpoint"]["emitted"])

    def test_missing_or_invalid_enforcement_inputs_fail_before_any_output_write(self):
        self.frontier.write_text("old frontier")
        self.report.write_text("old report")
        for options in ([], ["--robots", str(self.manifest)], ["--user-agent", "AuditBot"],
                        ["--robots", str(self.manifest), "--user-agent", "*"],
                        ["--robots", str(self.root / "absent"), "--user-agent", "AuditBot"]):
            with self.subTest(options=options), \
                    patch("qawrench.frontier.cli.atomic_write", side_effect=AssertionError("write forbidden")):
                self.assertEqual(self.cli(*options, "--enforce-robots")[0], 2)
        self.manifest.write_text('{"version":1,"origins":{},"origins":{}}')
        self.assertEqual(self.cli(*self.options, "--enforce-robots")[0], 2)
        self.assertEqual(self.frontier.read_text(), "old frontier")
        self.assertEqual(self.report.read_text(), "old report")

    def test_enforced_dry_runs_report_acceptance_or_rejection_without_writing(self):
        self.frontier.write_text("old frontier")
        self.report.write_text("old report")
        self.checkpoint.write_text("old checkpoint")
        for url, expected in (("https://example.test/private", 2), ("https://example.test/public", 0)):
            self.write_requests(url)
            with self.subTest(url=url), \
                    patch("qawrench.frontier.cli.atomic_write", side_effect=AssertionError("write forbidden")):
                code, out, _ = self.cli(*self.options, "--enforce-robots", "--dry-run",
                                        "--checkpoint-out", str(self.checkpoint))
                self.assertEqual(code, expected)
                report = json.loads(out)
                self.assertEqual(report["status"], "rejected" if expected else "ok")
                self.assertEqual(report["robots"]["mode"], "offline-enforced")
                self.assertFalse(report["frontier_emitted"])
                self.assertFalse(report["checkpoint"]["emitted"])
        self.assertEqual(self.frontier.read_text(), "old frontier")
        self.assertEqual(self.report.read_text(), "old report")
        self.assertEqual(self.checkpoint.read_text(), "old checkpoint")

    def test_enforcement_rechecks_collapsed_historical_original_before_batch_emission(self):
        self.write_requests("https://example.test/public", "https://example.test/public?secret=1")
        policy = self.root / "policy.json"
        policy.write_text('{"remove_query_keys":["secret"],"allow_ambiguous_merges":true}')
        self.assertEqual(self.cli("--policy", str(policy), "--checkpoint-out", str(self.checkpoint))[0], 0)
        self.assertEqual(len(self.frontier.read_text().splitlines()), 1)
        saved = self.frontier.read_bytes(), self.checkpoint.read_bytes()
        self.manifest.write_text(json.dumps({"version": 1, "origins": {"https://example.test": {
            "status": "available", "source": "synthetic:current",
            "text": "User-agent: *\nDisallow: /public?secret=\n"}}}))
        self.write_requests("https://example.test/new")
        next_checkpoint = self.root / "next.json"
        next_checkpoint.write_text("old next checkpoint")
        options = ["--checkpoint-in", str(self.checkpoint), "--checkpoint-out", str(next_checkpoint)]
        code, _, _ = self.cli(*options, *self.options, "--enforce-robots")
        self.assertEqual(code, 2)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["robots"]["counts"], {"allow": 2, "disallow": 1, "unknown": 0})
        denied = report["robots"]["origins"][0]["requests"][1]
        self.assertEqual((denied["url"], denied["location"]),
                         ("https://example.test/public?secret=1", "batch 1 line 2"))
        self.assertEqual(report["counts"]["accepted"], 2)
        self.assertFalse(report["checkpoint"]["emitted"])
        self.assertEqual((self.frontier.read_bytes(), self.checkpoint.read_bytes()), saved)
        self.assertEqual(next_checkpoint.read_text(), "old next checkpoint")
        # Enforcement is explicit each invocation, never inherited from saved state.
        self.assertEqual(self.cli(*options, *self.options)[0], 0)

    def test_enforcement_remains_independent_of_invalid_record_and_merge_permissions(self):
        self.write_requests("https://example.test/public")
        self.input.write_text(self.input.read_text() + "broken JSON\n")
        self.assertEqual(self.cli(*self.options, "--enforce-robots")[0], 2)
        self.assertEqual(json.loads(self.report.read_text())["robots"]["counts"]["allow"], 1)
        self.assertEqual(self.cli(*self.options, "--enforce-robots", "--on-invalid", "skip")[0], 0)
        saved = self.frontier.read_bytes()
        self.write_requests("https://example.test/private")
        self.input.write_text(self.input.read_text() + "broken JSON\n")
        self.assertEqual(self.cli(*self.options, "--enforce-robots", "--on-invalid", "skip")[0], 2)
        self.assertEqual(self.frontier.read_bytes(), saved)
        self.write_requests("https://example.test/public#a", "https://example.test/public#b")
        policy = self.root / "policy.json"
        policy.write_text('{"fragment":"drop"}')
        self.assertEqual(self.cli(*self.options, "--enforce-robots", "--policy", str(policy))[0], 2)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["robots"]["counts"]["allow"], 2)
        self.assertIn("allow_ambiguous_merges", report["errors"][0])
        self.assertEqual(self.frontier.read_bytes(), saved)

    def test_library_enforcement_is_strict_and_returns_candidate_with_rejection_report(self):
        robots = RobotsPolicy.from_dict(json.loads(self.manifest.read_text()))
        requests, issues, total = load_requests(self.input)
        for value in (None, 0, 1, "false"):
            with self.subTest(value=value), self.assertRaisesRegex(AuditError, "boolean"):
                audit(requests, Policy(), robots=robots, user_agent="AuditBot", enforce_robots=value)
        with self.assertRaisesRegex(AuditError, "requires"):
            audit([], Policy(), enforce_robots=True)
        kwargs = dict(issues=issues, total=total, robots=robots, user_agent="AuditBot")
        candidate, report = audit(requests, Policy(), enforce_robots=True, **kwargs)
        self.assertEqual(candidate, audit(requests, Policy(), **kwargs)[0])
        self.assertEqual(report["status"], "rejected")
        self.assertFalse(report["frontier_emitted"])
        self.assertEqual(audit(requests, Policy(), enforce_robots=True, **kwargs), (candidate, report))


if __name__ == "__main__":
    unittest.main()
