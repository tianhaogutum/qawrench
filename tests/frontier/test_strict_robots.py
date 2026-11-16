"""Opt-in malformed-rule gate; tolerant defaults and extension records stay usable.

Strict mode requires enforcement plus local manifest/product token. All three
malformed-rule reasons in encountered snapshots reject the whole candidate,
with original diagnostics and scoped witnesses. Unused origins are not audited.
Rejected runs only write the report; every dry run writes nothing.
"""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qawrench.frontier import AuditError, Policy, RobotsPolicy, audit, load_requests
from qawrench.frontier.cli import atomic_write, main


class StrictRobotsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "requests.jsonl"
        self.input.write_text('{"url":"https://example.test/public"}\n')
        self.manifest = self.root / "robots.json"
        self.frontier = self.root / "frontier.jsonl"
        self.report = self.root / "report.json"
        self.state = self.root / "state.json"
        self.options = ["--robots", str(self.manifest), "--user-agent", "AuditBot"]
        self.strict = [*self.options, "--enforce-robots", "--strict-robots"]
        self.snapshot("User-agent: AuditBot\nDisallow: no-leading-slash\n")

    def snapshot(self, text, extra=None):
        self.manifest.write_text(json.dumps({"version": 1, "origins": {
            "https://example.test": {"status": "available", "source": "synthetic:strict",
                                     "text": text}, **(extra or {})}}), encoding="utf-8")

    def cli(self, *options):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main([str(self.input), "--frontier", str(self.frontier),
                             "--report", str(self.report), *options])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_malformed_allow_decision_rejects_with_scoped_original_diagnostic(self):
        self.frontier.write_bytes(b"old frontier\n")
        self.state.write_bytes(b"old checkpoint\n")
        with patch("qawrench.frontier.cli.atomic_write", wraps=atomic_write) as writer, \
                patch("socket.socket", side_effect=AssertionError("network forbidden")):
            code, _, err = self.cli(*self.strict, "--checkpoint-out", str(self.state))
        self.assertEqual(code, 2)
        self.assertTrue(self.report.exists(), err)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["status"], "rejected")
        self.assertFalse(report["frontier_emitted"])
        self.assertFalse(report["checkpoint"]["emitted"])
        self.assertEqual(report["counts"]["accepted"], 1)  # simulated candidate
        robots = report["robots"]
        self.assertEqual(robots["mode"], "offline-enforced")
        self.assertTrue(robots["strict"])
        self.assertEqual(robots["counts"], {"allow": 1, "disallow": 0, "unknown": 0})
        diagnostic = {"line": 2, "text": "Disallow: no-leading-slash", "reason": "invalid path pattern"}
        self.assertEqual(robots["origins"][0]["diagnostics"], [diagnostic])
        self.assertEqual(robots["malformed_rules"], [
            {"origin": "https://example.test", "source": "synthetic:strict", **diagnostic,
             "snapshot_sha256": hashlib.sha256(
                 b"User-agent: AuditBot\nDisallow: no-leading-slash\n").hexdigest(),
             "byte_start": 21, "byte_end": 47}])
        self.assertIn("strict robots", err)
        self.assertEqual([c.args[0] for c in writer.call_args_list], [self.report])
        self.assertEqual(self.frontier.read_bytes(), b"old frontier\n")
        self.assertEqual(self.state.read_bytes(), b"old checkpoint\n")

    def test_all_malformed_categories_reject_even_in_unselected_groups(self):
        for text, expected in [
            ("User-agent: AuditBot\nAllow: no-slash\n", [(2, "invalid path pattern")]),
            ("User-agent: AuditBot\nDisallow: /bad%xx\n", [(2, "invalid path pattern")]),
            ("User-agent: AuditBot\nDisallow: /bad path\n", [(2, "invalid path pattern")]),
            ("User-agent: AuditBot\nUser-agent: BadBot/1\nAllow: /\n", [(2, "invalid user-agent token")]),
            ("User-agent: BadBot/1\nDisallow: /\n", [(1, "invalid user-agent token"),
                                                        (2, "rule outside a valid group")]),
            ("Disallow: /orphan\nUser-agent: AuditBot\nAllow: /\n", [(1, "rule outside a valid group")]),
            ("User-agent: AuditBot\nAllow: /\nUser-agent: OtherBot\nDisallow: bad\n",
             [(4, "invalid path pattern")])]:
            with self.subTest(text=text):
                self.snapshot(text)
                self.assertEqual(self.cli(*self.strict, "--checkpoint-out", str(self.state))[0], 2)
                robots = json.loads(self.report.read_text())["robots"]
                self.assertEqual([(d["line"], d["reason"]) for d in robots["malformed_rules"]], expected)
                self.assertEqual(robots["counts"]["allow"], 1)
                self.assertFalse(self.frontier.exists())
                self.assertFalse(self.state.exists())

    def test_default_informational_and_enforced_modes_remain_tolerant(self):
        for flags, mode in ((self.options, "offline-informational"),
                            ([*self.options, "--enforce-robots"], "offline-enforced")):
            with self.subTest(mode=mode):
                self.assertEqual(self.cli(*flags)[0], 0)
                report = json.loads(self.report.read_text())
                self.assertEqual(report["status"], "ok")
                self.assertTrue(report["frontier_emitted"])
                self.assertEqual(report["robots"]["mode"], mode)
                self.assertNotIn("strict", report["robots"])
                self.assertNotIn("malformed_rules", report["robots"])
                self.assertEqual(report["robots"]["origins"][0]["diagnostics"][0]["reason"],
                                 "invalid path pattern")

    def test_extensions_do_not_block_or_split_valid_groups(self):
        self.snapshot("Sitemap: https://example.test/map.xml\nUser-agent: AuditBot\n"
                      "Disallow: /\nFuture: extension\nAllow: /public\nCrawl-delay: 10\n")
        self.assertEqual(self.cli(*self.strict, "--checkpoint-out", str(self.state))[0], 0)
        report = json.loads(self.report.read_text())
        self.assertTrue(report["frontier_emitted"])
        self.assertTrue(report["checkpoint"]["emitted"])
        self.assertEqual(report["robots"]["malformed_rules"], [])
        origin = report["robots"]["origins"][0]
        self.assertEqual([d["line"] for d in origin["diagnostics"]], [1, 4, 6])
        self.assertTrue(all(d["reason"] == "ignored non-rule record" for d in origin["diagnostics"]))
        self.assertEqual(origin["requests"][0]["winning_rule"]["line"], 5)

    def test_strict_requires_enforcement_and_its_valid_inputs_before_writes(self):
        for options in ([], self.options, ["--enforce-robots"],
                        ["--robots", str(self.manifest), "--enforce-robots"],
                        ["--user-agent", "AuditBot", "--enforce-robots"],
                        ["--robots", str(self.manifest), "--user-agent", "*", "--enforce-robots"]):
            with self.subTest(options=options), \
                    patch("qawrench.frontier.cli.atomic_write", side_effect=AssertionError("write forbidden")):
                self.assertEqual(self.cli(*options, "--strict-robots")[0], 2)
                self.assertFalse(self.report.exists())
                self.assertFalse(self.frontier.exists())

    def test_dry_runs_never_create_or_replace_outputs(self):
        for existing in (False, True):
            if existing:
                for path in (self.frontier, self.report, self.state):
                    path.write_bytes(b"original\n")
            for text, expected in (("User-agent: AuditBot\nDisallow: bad\n", 2),
                                   ("User-agent: AuditBot\nAllow: /\nSitemap: map\n", 0)):
                with self.subTest(existing=existing, expected=expected):
                    self.snapshot(text)
                    before = {p: p.read_bytes() for p in self.root.iterdir()}
                    with patch("qawrench.frontier.cli.atomic_write", side_effect=AssertionError("write forbidden")):
                        code, out, _ = self.cli(*self.strict, "--dry-run", "--checkpoint-out", str(self.state))
                    self.assertEqual(code, expected)
                    report = json.loads(out)
                    self.assertEqual(report["status"], "rejected" if expected else "ok")
                    self.assertFalse(report["frontier_emitted"])
                    self.assertFalse(report["checkpoint"]["emitted"])
                    self.assertEqual({p: p.read_bytes() for p in self.root.iterdir()}, before)

    def test_current_strict_gate_rechecks_historical_origin_on_replay_and_append(self):
        for batch_id in (None, "seed:a"):
            with self.subTest(batch_id=batch_id):
                self.input.write_text('{"url":"https://example.test/public"}\n')
                named = ["--batch-id", batch_id] if batch_id else []
                self.assertEqual(self.cli("--checkpoint-out", str(self.state), *named)[0], 0)
                saved = self.state.read_bytes(), self.frontier.read_bytes()
                next_state = self.root / "next.json"
                next_state.write_bytes(b"old next checkpoint\n")
                # Anonymous case appends a different origin; named case replays.
                if batch_id is None:
                    self.input.write_text('{"url":"https://other.test/new"}\n')
                self.snapshot("User-agent: AuditBot\nDisallow: bad\n", {
                    "https://other.test": {"status": "available", "source": "other", "text": ""}})
                options = ["--checkpoint-in", str(self.state), "--checkpoint-out", str(next_state), *named]
                self.assertEqual(self.cli(*options, *self.strict)[0], 2)
                report = json.loads(self.report.read_text())
                self.assertEqual(report["robots"]["malformed_rules"][0]["origin"], "https://example.test")
                self.assertEqual(report["checkpoint"]["incoming_batch"]["status"],
                                 "replayed" if batch_id else "appended")
                self.assertEqual((self.state.read_bytes(), self.frontier.read_bytes()), saved)
                self.assertEqual(next_state.read_bytes(), b"old next checkpoint\n")
                # Strictness is never inherited from state.
                self.assertEqual(self.cli(*options, *self.options, "--enforce-robots")[0], 0)

    def test_only_encountered_origins_are_checked_and_empty_available_rules_pass(self):
        self.snapshot("", {"https://unused.test": {"status": "available", "source": "unused",
                                                  "text": "User-agent: BadBot/1\n"}})
        self.assertEqual(self.cli(*self.strict)[0], 0)
        robots = json.loads(self.report.read_text())["robots"]
        self.assertEqual(len(robots["origins"]), 1)
        self.assertEqual(robots["malformed_rules"], [])

    def test_strict_gate_does_not_bypass_disallow_unknown_or_other_gates(self):
        self.input.write_text(self.input.read_text() + "broken JSON\n")
        for text, extra in (("User-agent: AuditBot\nDisallow: /\n", {}),
                            ("User-agent: AuditBot\nDisallow: /\nAllow: bad\n", {}),
                            ("", {"https://example.test": {"status": "unavailable", "source": "missing",
                                                            "reason": "not captured"}}),
                            ("User-agent: AuditBot\nDisallow: bad\n", {})):
            with self.subTest(text=text, extra=extra):
                self.snapshot(text, extra)
                self.assertEqual(self.cli(*self.strict, "--on-invalid", "skip")[0], 2)
                self.assertFalse(self.frontier.exists())
                if "Allow: bad" in text:
                    report = json.loads(self.report.read_text())
                    self.assertEqual(report["robots"]["counts"]["disallow"], 1)
                    self.assertEqual(len(report["robots"]["malformed_rules"]), 1)
                    self.assertEqual(len(report["errors"]), 2)  # both gates retained
        self.snapshot("User-agent: AuditBot\nAllow: /\n")
        self.assertEqual(self.cli(*self.strict)[0], 2)  # invalid request still rejects
        self.assertEqual(self.cli(*self.strict, "--on-invalid", "skip")[0], 0)

    def test_library_requires_boolean_and_enforcement_returns_candidate_and_complete_report(self):
        requests, _, _ = load_requests(self.input)
        robots = RobotsPolicy.from_dict(json.loads(self.manifest.read_text()))
        kwargs = dict(robots=robots, user_agent="AuditBot")
        for value in (None, 0, 1, "false"):
            with self.subTest(value=value), self.assertRaisesRegex(AuditError, "strict_robots must be boolean"):
                audit(requests, Policy(), strict_robots=value, **kwargs)
        with self.assertRaisesRegex(AuditError, "requires.*enforce_robots"):
            audit(requests, Policy(), strict_robots=True, **kwargs)
        with self.assertRaisesRegex(AuditError, "requires"):
            audit([], Policy(), strict_robots=True, enforce_robots=True)
        candidate, report = audit(requests, Policy(), strict_robots=True, enforce_robots=True, **kwargs)
        self.assertEqual(candidate, audit(requests, Policy(), **kwargs)[0])
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(audit(requests, Policy(), strict_robots=True, enforce_robots=True, **kwargs),
                         (candidate, report))


if __name__ == "__main__":
    unittest.main()
