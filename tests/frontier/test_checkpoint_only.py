"""Checkpoint-only audits retain history and apply every current rejection gate."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qawrench.frontier import Checkpoint, Policy, audit
from qawrench.frontier.cli import main
from qawrench.frontier.core import json_text


class CheckpointOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "saved.json"
        self.next_state = self.root / "next.json"
        self.frontier = self.root / "frontier.jsonl"
        self.report = self.root / "report.json"
        self.policy = self.root / "policy.json"
        self.robots = self.root / "robots.json"
        self.url = "https://example.test/submit"
        self.text = json_text({"url": self.url, "method": "POST", "body": "a=1", "source": "synthetic:a"}) + "\r\n"

    def save(self, *, named=False, invalid=False, policy=None):
        state = Checkpoint(policy or Policy()).append(self.text + ("not JSON\r\n" if invalid else ""))
        state = state.append(json.dumps([{"url": self.url, "method": "POST", "payload": "a=1",
                                         "label": "synthetic:b"}, self.url]), "crawlee",
                             batch_id="seed:b" if named else None)
        self.state.write_text(json_text(state.to_dict()) + "\n", encoding="utf-8")
        return state

    def cli(self, *options, mode=True, checkpoint=True):
        out, err = io.StringIO(), io.StringIO()
        args = (["--checkpoint-only"] if mode else [])
        args += (["--checkpoint-in", str(self.state)] if checkpoint else [])
        args += ["--frontier", str(self.frontier), "--report", str(self.report), *options]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(args)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def assert_saved_report(self, report, state, *, on_invalid="fail"):
        requests, issues, total = state.load_requests()
        _, expected = audit(requests, state.policy, issues=issues, total=total, on_invalid=on_invalid)
        for field in ("counts", "representatives", "collision_groups", "issues"):
            self.assertEqual(report[field], expected[field])
        self.assertEqual(report["checkpoint"]["batches"], len(state.batches))
        self.assertEqual(report["checkpoint"]["previous_batches"], len(state.batches))
        self.assertEqual(report["checkpoint"]["incoming_batch"], {"batch_id": None, "status": "not_supplied"})
        self.assertEqual(report["checkpoint"]["batch_conflicts"], [])

    def test_no_positional_input_audits_saved_history(self):
        state = self.save()
        # This failed on the prior CLI because the positional input was required.
        code, out, err = self.cli("--dry-run")
        self.assertEqual(code, 0, err)
        self.assert_saved_report(json.loads(out), state)

    def test_version_1_and_2_preserve_serialized_history_and_witnesses(self):
        for named in (False, True):
            with self.subTest(named=named):
                state = self.save(named=named)
                saved = self.state.read_bytes()
                with patch("qawrench.frontier.checkpoint.Checkpoint.append", side_effect=AssertionError("no append")), \
                        patch("socket.socket", side_effect=AssertionError("network forbidden")):
                    code, _, err = self.cli("--checkpoint-out", str(self.next_state))
                self.assertEqual(code, 0, err)
                report = json.loads(self.report.read_text())
                self.assert_saved_report(report, state)
                self.assertEqual(report["checkpoint"]["version"], 2 if named else 1)
                self.assertTrue(report["checkpoint"]["emitted"])
                self.assertEqual(self.next_state.read_bytes(), saved)
                self.assertEqual(self.state.read_bytes(), saved)
                members = report["collision_groups"][0]["members"]
                self.assertEqual([m["index"] for m in members], [1, 2])
                self.assertEqual([m["location"] for m in members], ["batch 1 line 1", "batch 2 item 1"])
                self.assertEqual(len(self.frontier.read_text().splitlines()), 2)
                # A one-off re-audit need not emit a new checkpoint.
                self.assertEqual(self.cli()[0], 0)
                self.assertFalse(json.loads(self.report.read_text())["checkpoint"]["emitted"])

    def test_ambiguous_cli_combinations_fail_before_reads_or_writes(self):
        self.save()
        for p in (self.frontier, self.report, self.next_state):
            p.write_bytes(b"sentinel\n")
        saved = {p: p.read_bytes() for p in self.root.iterdir()}
        variants = [([], False, True, "requires --checkpoint-in"),
                    ([str(self.root / "missing-batch")], True, True, "cannot be combined with input"),
                    (["--batch-id", "seed:b"], True, True, "cannot be combined with --batch-id"),
                    (["--format", "jsonl"], True, True, "cannot be combined with --format"),
                    ([], True, False, "input is required unless --checkpoint-only")]
        for options, checkpoint, mode, message in variants:
            for dry in ([], ["--dry-run"]):
                with self.subTest(options=options, checkpoint=checkpoint, mode=mode, dry=dry), \
                        patch("pathlib.Path.read_text", side_effect=AssertionError("read forbidden")), \
                        patch("qawrench.frontier.cli.atomic_write", side_effect=AssertionError("write forbidden")):
                    code, _, err = self.cli(*options, *dry, checkpoint=checkpoint, mode=mode)
                self.assertEqual(code, 2)
                self.assertIn(message, err)
        self.assertEqual({p: p.read_bytes() for p in self.root.iterdir()}, saved)

    def test_saved_issues_require_explicit_skip_and_keep_cumulative_indices(self):
        state = self.save(named=True, invalid=True)
        self.frontier.write_bytes(b"old frontier\n")
        self.next_state.write_bytes(b"old next\n")
        saved = self.state.read_bytes(), self.frontier.read_bytes(), self.next_state.read_bytes()
        self.assertEqual(self.cli("--checkpoint-out", str(self.next_state))[0], 2)
        report = json.loads(self.report.read_text())
        self.assert_saved_report(report, state)
        self.assertEqual(report["issues"][0]["location"], "batch 1 line 2")
        self.assertEqual(report["collision_groups"][0]["members"][1]["index"], 3)
        self.assertEqual((self.state.read_bytes(), self.frontier.read_bytes(), self.next_state.read_bytes()), saved)
        self.assertEqual(self.cli("--on-invalid", "skip", "--checkpoint-out", str(self.next_state))[0], 0)
        self.assert_saved_report(json.loads(self.report.read_text()), state, on_invalid="skip")
        self.assertEqual(self.next_state.read_bytes(), self.state.read_bytes())

    def test_policy_conflict_and_saved_ambiguity_still_reject(self):
        state = self.save()
        self.policy.write_text('{"fragment":"drop"}')
        for p in (self.frontier, self.next_state):
            p.write_bytes(b"old output\n")
        saved = self.frontier.read_bytes(), self.next_state.read_bytes()
        self.assertEqual(self.cli("--policy", str(self.policy), "--checkpoint-out", str(self.next_state))[0], 2)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["checkpoint"]["policy_conflicts"], [
            {"field": "fragment", "checkpoint": "preserve", "requested": "drop"}])
        self.assertEqual(report["counts"]["raw_records"], 3)
        state = Checkpoint(Policy(fragment="drop")).append(
            '{"url":"https://example.test/#a"}\n{"url":"https://example.test/#b"}\n')
        self.state.write_text(json_text(state.to_dict()) + "\n")
        self.assertEqual(self.cli("--checkpoint-out", str(self.next_state))[0], 2)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["counts"]["policy_created_collision_groups"], 1)
        self.assertEqual(report["checkpoint"]["policy_conflicts"], [])
        self.assertEqual(report["status"], "rejected")
        self.assertEqual((self.frontier.read_bytes(), self.next_state.read_bytes()), saved)

    def write_robots(self, text):
        self.robots.write_text(json.dumps({"version": 1, "origins": {"https://example.test": {
            "status": "available", "source": "synthetic:current", "text": text}}}))
        return ["--robots", str(self.robots), "--user-agent", "AuditBot"]

    def test_current_robots_informational_enforced_unknown_and_strict_gates(self):
        state = self.save(named=True)
        options = self.write_robots("User-agent: *\nDisallow: /\n")
        self.assertEqual(self.cli(*options)[0], 0)  # informational default
        self.next_state.write_bytes(b"old next\n")
        saved = self.frontier.read_bytes(), self.next_state.read_bytes(), self.state.read_bytes()
        for text, strict, decision in [("User-agent: *\nDisallow: /\n", [], "disallow"),
                                       (None, [], "unknown"),
                                       ("User-agent: *\nDisallow: no-slash\n", ["--strict-robots"], "allow")]:
            with self.subTest(decision=decision):
                if text is None:
                    self.robots.write_text('{"version":1,"origins":{}}')
                else:
                    self.write_robots(text)
                self.assertEqual(self.cli(*options, "--enforce-robots", *strict,
                                          "--checkpoint-out", str(self.next_state))[0], 2)
                report = json.loads(self.report.read_text())
                self.assert_saved_report(report, state)
                self.assertEqual(report["robots"]["counts"][decision], 3)  # includes duplicate originals
                if strict:
                    self.assertEqual(report["robots"]["malformed_rules"][0]["line"], 2)
                self.assertFalse(report["frontier_emitted"])
                self.assertFalse(report["checkpoint"]["emitted"])
                self.assertEqual((self.frontier.read_bytes(), self.next_state.read_bytes(), self.state.read_bytes()), saved)

    def test_accepted_and_rejected_dry_runs_write_nothing_existing_or_absent(self):
        state = self.save(named=True)
        options = self.write_robots("User-agent: *\nDisallow: /\n")
        for existing in (False, True):
            for p in (self.frontier, self.report, self.next_state):
                if existing:
                    p.write_bytes(b"sentinel\n")
                else:
                    p.unlink(missing_ok=True)
            before = {p: p.read_bytes() for p in self.root.iterdir()}
            for extra, expected in [([], 0), ([*options, "--enforce-robots"], 2)]:
                with self.subTest(existing=existing, expected=expected), \
                        patch("qawrench.frontier.cli.atomic_write", side_effect=AssertionError("write forbidden")):
                    code, out, err = self.cli("--dry-run", "--checkpoint-out", str(self.next_state), *extra)
                self.assertEqual(code, expected, err)
                report = json.loads(out)
                self.assert_saved_report(report, state)
                self.assertFalse(report["frontier_emitted"])
                self.assertFalse(report["checkpoint"]["emitted"])
                self.assertEqual({p: p.read_bytes() for p in self.root.iterdir()}, before)

    def test_rejected_audit_preserves_absent_outputs(self):
        self.save(invalid=True)
        self.assertEqual(self.cli("--checkpoint-out", str(self.next_state))[0], 2)
        self.assertFalse(self.frontier.exists())
        self.assertFalse(self.next_state.exists())
        self.assertEqual(json.loads(self.report.read_text())["status"], "rejected")

    def test_malformed_checkpoint_and_output_aliases_fail_without_writes(self):
        self.save()
        self.frontier.write_bytes(b"old frontier\n")
        self.report.write_bytes(b"old report\n")
        saved = self.state.read_bytes(), self.frontier.read_bytes(), self.report.read_bytes()
        for target in (self.state, self.frontier, self.report):
            with self.subTest(target=target):
                self.assertEqual(self.cli("--checkpoint-out", str(target))[0], 2)
        for link in ("hardlink", "symlink"):
            if link == "hardlink":
                self.next_state.hardlink_to(self.state)
            else:
                self.next_state.symlink_to(self.state)
            self.assertEqual(self.cli("--checkpoint-out", str(self.next_state))[0], 2)
            self.next_state.unlink()
        self.assertEqual((self.state.read_bytes(), self.frontier.read_bytes(), self.report.read_bytes()), saved)
        for text in ('{"version":1}', '{"version":1,"version":1}', 'not JSON'):
            self.state.write_text(text)
            with patch("qawrench.frontier.cli.atomic_write", side_effect=AssertionError("write forbidden")):
                self.assertEqual(self.cli("--checkpoint-out", str(self.next_state))[0], 2)
            self.assertEqual((self.frontier.read_bytes(), self.report.read_bytes()), saved[1:])
            self.assertFalse(self.next_state.exists())

    def test_version_2_anonymous_only_retains_existing_canonicalization(self):
        state = self.save()
        data = state.to_dict()
        data["version"] = 2
        for batch in data["batches"]:
            batch["batch_id"] = None
        self.state.write_text(json.dumps(data, indent=2))
        self.assertEqual(self.cli("--checkpoint-out", str(self.next_state))[0], 0)
        self.assertEqual(self.next_state.read_text(), json_text(state.to_dict()) + "\n")
        self.assert_saved_report(json.loads(self.report.read_text()), state)


if __name__ == "__main__":
    unittest.main()
