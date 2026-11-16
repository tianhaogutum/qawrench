"""Colonless core records block strict emission while tolerant parsing stays intact."""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from qawrench.frontier import Checkpoint, Policy, RobotsPolicy, audit, load_requests
from qawrench.frontier.cli import atomic_write, main
from qawrench.frontier.core import json_text


class ColonlessRobotsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "requests.jsonl"
        self.input.write_text('{"url":"https://example.test/private/public"}\n')
        self.manifest = self.root / "robots.json"
        self.frontier = self.root / "frontier.jsonl"
        self.report = self.root / "report.json"
        self.state = self.root / "next.json"
        self.options = ["--robots", str(self.manifest), "--user-agent", "AuditBot"]
        self.strict = [*self.options, "--enforce-robots", "--strict-robots"]
        self.snapshot("User-agent: AuditBot\nDisallow /private\n")

    def snapshot(self, text, extra=None):
        self.snapshot_text = text
        self.manifest.write_text(json.dumps({"version": 1, "origins": {
            "https://example.test": {"status": "available", "source": "synthetic:colonless",
                                     "text": text}, **(extra or {})}}), encoding="utf-8")

    def cli(self, *options, history=False):
        out, err = io.StringIO(), io.StringIO()
        args = [] if history else [str(self.input)]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([*args, "--frontier", str(self.frontier), "--report", str(self.report), *options])
        return code, out.getvalue(), err.getvalue()

    def witness(self, line, text):
        records = re.split(r'(\r\n|\r|\n)', self.snapshot_text)
        start = len(''.join(records[:2 * (line - 1)]).encode('utf-8'))
        binding = {'snapshot_sha256': hashlib.sha256(self.snapshot_text.encode('utf-8')).hexdigest(),
                   'byte_start': start, 'byte_end': start + len(text.encode('utf-8'))}
        return {"origin": "https://example.test", "source": "synthetic:colonless", "line": line,
                "text": text, "reason": "missing colon after core directive", **binding}

    def test_colonless_disallow_rejects_and_preserves_outputs(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    self.frontier.write_bytes(b"old frontier\n")
                    self.state.write_bytes(b"old checkpoint\n")
                with patch("qawrench.frontier.cli.atomic_write", wraps=atomic_write) as writer:
                    code, _, err = self.cli(*self.strict, "--checkpoint-out", str(self.state))
                self.assertEqual(code, 2, err)
                report = json.loads(self.report.read_text())
                self.assertEqual(report["status"], "rejected")
                self.assertEqual(report["robots"]["counts"], {"allow": 1, "disallow": 0, "unknown": 0})
                self.assertEqual(report["robots"]["malformed_rules"], [self.witness(2, "Disallow /private")])
                self.assertEqual(report["robots"]["origins"][0]["diagnostics"], [
                    {"line": 2, "text": "Disallow /private", "reason": "ignored non-rule record"}])
                self.assertEqual(report["counts"]["accepted"], 1)
                self.assertFalse(report["frontier_emitted"])
                self.assertFalse(report["checkpoint"]["emitted"])
                self.assertEqual([c.args[0] for c in writer.call_args_list], [self.report])
                if existing:
                    self.assertEqual(self.frontier.read_bytes(), b"old frontier\n")
                    self.assertEqual(self.state.read_bytes(), b"old checkpoint\n")
                else:
                    self.assertFalse(self.frontier.exists())
                    self.assertFalse(self.state.exists())

    def test_exact_core_names_case_whitespace_comments_and_colons_inside_values(self):
        for record in ("User-agent AuditBot", "Allow /private", "Disallow /private",
                       " \tUsEr-AgEnT\tOtherBot \t# original comment", "\tAlLoW \t/private\t ",
                       " dIsAlLoW\t/private # comment", "Disallow /a:b", "User-agent Bot:Name",
                       "Allow /https://example.test", "User-agent", " Allow\t", "Disallow # no value"):
            with self.subTest(record=record):
                self.snapshot("\ufeffUser-agent: AuditBot\r\n" + record + "\rAllow: /\r")
                self.assertEqual(self.cli(*self.strict)[0], 2)
                robots = json.loads(self.report.read_text())["robots"]
                self.assertEqual(robots["malformed_rules"], [self.witness(2, record)])
                self.assertEqual(robots["origins"][0]["requests"][0]["winning_rule"]["line"], 3)
                self.assertEqual(robots["origins"][0]["selected_agents"], [{"line": 1, "value": "AuditBot"}])

    def test_valid_colons_and_extensions_are_nonblocking_and_do_not_split_groups(self):
        extensions = ["Sitemap: https://example.test/map.xml", "Future: Disallow /private",
                      "Crawl-delay: 10", "Sitemap https://example.test/map.xml", "Crawl-delay 10",
                      "Disallow-extra /private", "Allowance /private", "User-agentish AuditBot",
                      "X-Disallow /private", "Disallow/private", "Allow= /private", "Disallow; /private",
                      "Dİsallow /private", "Dısallow /private", "Disallow\u00a0/private", "# Disallow /private"]
        self.snapshot(" \tUsEr-AgEnT \t: AuditBot\nDisallow\t: /private\n" +
                      "\n".join(extensions) + "\n\tAlLoW \t: /private/public\nDisallow :\n")
        self.assertEqual(self.cli(*self.strict)[0], 0)
        robots = json.loads(self.report.read_text())["robots"]
        self.assertEqual(robots["malformed_rules"], [])
        origin = robots["origins"][0]
        self.assertEqual(len(origin["diagnostics"]), len(extensions) - 1)
        self.assertTrue(all(d["reason"] == "ignored non-rule record" for d in origin["diagnostics"]))
        self.assertEqual(origin["requests"][0]["winning_rule"]["line"], len(extensions) + 3)
        self.assertEqual(len(origin["requests"][0]["matched_rules"]), 2)

    def test_default_modes_retain_ignored_diagnostics_matching_and_report_shape(self):
        self.snapshot("User-agent: AuditBot\nDisallow /private\nAllow /private/public\n"
                      "User-agent OtherBot\nDisallow: /private\nAllow: /private/public\n")
        for options, mode in ((self.options, "offline-informational"),
                              ([*self.options, "--enforce-robots"], "offline-enforced")):
            with self.subTest(mode=mode):
                self.assertEqual(self.cli(*options)[0], 0)
                robots = json.loads(self.report.read_text())["robots"]
                self.assertEqual(set(robots), {"user_agent", "mode", "counts", "origins"})
                self.assertEqual(robots["mode"], mode)
                origin = robots["origins"][0]
                self.assertEqual(origin["diagnostics"], [
                    {"line": n, "text": t, "reason": "ignored non-rule record"}
                    for n, t in [(2, "Disallow /private"), (3, "Allow /private/public"), (4, "User-agent OtherBot")]])
                self.assertEqual(origin["selected_agents"], [{"line": 1, "value": "AuditBot"}])
                self.assertEqual(origin["requests"][0]["winning_rule"]["line"], 6)
                self.assertEqual(robots["counts"], {"allow": 1, "disallow": 0, "unknown": 0})
        requests, _, _ = load_requests(self.input)
        robots = RobotsPolicy.from_dict(json.loads(self.manifest.read_text()))
        candidate, tolerant = audit(requests, Policy(), robots=robots, user_agent="AuditBot", enforce_robots=True)
        strict_candidate, strict = audit(requests, Policy(), robots=robots, user_agent="AuditBot",
                                         enforce_robots=True, strict_robots=True)
        self.assertEqual(candidate, strict_candidate)
        strict_robots = dict(strict["robots"])
        strict_robots.pop("strict")
        strict_robots.pop("malformed_rules")
        self.assertEqual(tolerant["robots"], strict_robots)
        self.assertEqual(len(strict["robots"]["malformed_rules"]), 3)

    def test_scope_includes_other_groups_but_excludes_unused_origins(self):
        self.snapshot("User-agent: AuditBot\nAllow: /\nUser-agent: OtherBot\nDisallow /private\n", {
            "https://unused.test": {"status": "available", "source": "unused", "text": "Allow /bad\n"}})
        self.assertEqual(self.cli(*self.strict)[0], 2)
        robots = json.loads(self.report.read_text())["robots"]
        self.assertEqual(robots["malformed_rules"], [self.witness(4, "Disallow /private")])
        self.assertEqual(len(robots["origins"]), 1)
        self.assertEqual(robots["counts"]["allow"], 1)

    def test_accepted_and_rejected_dry_runs_never_write(self):
        for existing in (False, True):
            if existing:
                for path in (self.frontier, self.report, self.state):
                    path.write_bytes(b"original\n")
            for text, expected in (("User-agent: AuditBot\nDisallow /private\n", 2),
                                   ("User-agent: AuditBot\nSitemap: https://example.test/map\n", 0)):
                with self.subTest(existing=existing, expected=expected):
                    self.snapshot(text)
                    before = {p: p.read_bytes() for p in self.root.iterdir()}
                    with patch("qawrench.frontier.cli.atomic_write", side_effect=AssertionError("write forbidden")):
                        code, out, _ = self.cli(*self.strict, "--dry-run", "--checkpoint-out", str(self.state))
                    self.assertEqual(code, expected)
                    self.assertEqual(json.loads(out)["status"], "rejected" if expected else "ok")
                    self.assertEqual({p: p.read_bytes() for p in self.root.iterdir()}, before)

    def test_checkpoint_only_append_and_replay_reaudit_history_without_emitting(self):
        saved = self.root / "saved.json"
        checkpoint = Checkpoint(Policy()).append(self.input.read_text(), "jsonl", batch_id="seed:a")
        saved.write_text(json_text(checkpoint.to_dict()) + "\n")
        for extra, history, status in ((["--checkpoint-only"], True, "not_supplied"),
                                       (["--batch-id", "seed:a"], False, "replayed"),
                                       ([], False, "appended")):
            with self.subTest(status=status):
                self.frontier.write_bytes(b"old frontier\n")
                self.state.write_bytes(b"old next\n")
                before = [p.read_bytes() for p in (saved, self.frontier, self.state)]
                self.assertEqual(self.cli(*self.strict, "--checkpoint-in", str(saved),
                                          "--checkpoint-out", str(self.state), *extra, history=history)[0], 2)
                report = json.loads(self.report.read_text())
                self.assertEqual(report["checkpoint"]["incoming_batch"]["status"], status)
                self.assertEqual(report["robots"]["malformed_rules"], [self.witness(2, "Disallow /private")])
                self.assertEqual([p.read_bytes() for p in (saved, self.frontier, self.state)], before)

    def test_other_rejection_gates_remain_present(self):
        self.input.write_text('{"url":"https://example.test/private/public#a"}\n'
                              '{"url":"https://example.test/private/public#b"}\n'
                              '{"url":"https://unknown.test/"}\nbroken JSON\n')
        self.snapshot("User-agent: AuditBot\nDisallow: /\nAllow no-colon\nDisallow: bad\n")
        policy = self.root / "policy.json"
        policy.write_text('{"fragment":"drop"}')
        saved = self.root / "saved.json"
        saved.write_text(json_text(Checkpoint(Policy()).append(self.input.read_text(), "jsonl").to_dict()))
        self.assertEqual(self.cli(*self.strict, "--policy", str(policy), "--checkpoint-in", str(saved),
                                  "--checkpoint-only", "--checkpoint-out", str(self.state), history=True)[0], 2)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["robots"]["counts"], {"allow": 0, "disallow": 2, "unknown": 1})
        self.assertEqual([d["reason"] for d in report["robots"]["malformed_rules"]],
                         ["missing colon after core directive", "invalid path pattern"])
        self.assertEqual(len(report["issues"]), 1)
        self.assertEqual(report["counts"]["policy_created_collision_groups"], 1)
        self.assertEqual(report["checkpoint"]["policy_conflicts"][0]["field"], "fragment")
        self.assertEqual(len(report["errors"]), 5)  # invalid, ambiguity, strict, decisions, saved policy
        self.assertFalse(self.frontier.exists())
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
