"""Checkpoint merge contracts using local synthetic crawl batches."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qawrench.frontier import Checkpoint, Policy, audit
from qawrench.frontier.cli import main
from qawrench.frontier.core import strict_json


class CheckpointCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.first = self.root / "a.jsonl"
        self.second = self.root / "b.jsonl"
        self.frontier = self.root / "frontier.jsonl"
        self.report = self.root / "report.json"
        self.checkpoint = self.root / "a.checkpoint.json"
        self.next_checkpoint = self.root / "b.checkpoint.json"
        self.url = "https://example.test/submit"
        self.a = [{"url": self.url, "source": "synthetic:a"},
                  {"url": self.url, "method": "POST", "body": "a=1", "source": "synthetic:a"}]
        self.b = [{"url": self.url, "method": "POST", "body": "a=1", "source": "synthetic:b"},
                  {"url": self.url, "method": "POST", "body": "a=2", "source": "synthetic:b"}]
        self.write_batch(self.first, self.a)
        self.write_batch(self.second, self.b)

    def write_batch(self, path, records):
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    def cli(self, batch, *options):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([str(batch), "--frontier", str(self.frontier), "--report", str(self.report), *options])
        return code, out.getvalue(), err.getvalue()

    def start(self, *options):
        self.assertEqual(self.cli(self.first, "--checkpoint-out", str(self.checkpoint), *options)[0], 0)

    def merge(self, *options):
        return self.cli(self.second, "--checkpoint-in", str(self.checkpoint),
                        "--checkpoint-out", str(self.next_checkpoint), *options)

    def test_two_batches_keep_method_body_and_all_duplicate_witnesses(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden")):
            self.start()
            saved = self.checkpoint.read_bytes()
            self.assertEqual(self.merge()[0], 0)
        frontier = [json.loads(line) for line in self.frontier.read_text().splitlines()]
        self.assertEqual([(r["method"], r["body"]) for r in frontier],
                         [("GET", ""), ("POST", "a=1"), ("POST", "a=2")])
        self.assertEqual(frontier[1]["source"], "synthetic:a")
        report = json.loads(self.report.read_text())
        self.assertEqual(report["counts"]["raw_records"], 4)
        self.assertEqual(report["counts"]["accepted"], 3)
        members = report["collision_groups"][0]["members"]
        self.assertEqual([m["index"] for m in members], [2, 3])
        self.assertEqual([m["location"] for m in members], ["batch 1 line 2", "batch 2 line 1"])
        self.assertEqual([m["original"] for m in members], [self.a[1], self.b[0]])
        self.assertEqual(report["checkpoint"]["batches"], 2)
        self.assertEqual(report["checkpoint"]["policy_conflicts"], [])
        self.assertTrue(report["checkpoint"]["emitted"])
        self.assertEqual(self.checkpoint.read_bytes(), saved)

    def test_inherited_policy_retains_previous_nonrepresentatives(self):
        policy = self.root / "policy.json"
        policy.write_text('{"remove_query_keys":["utm"],"allow_ambiguous_merges":true}')
        self.write_batch(self.first, [{"url": self.url + "?utm=a"}, {"url": self.url + "?utm=b"}])
        self.write_batch(self.second, [{"url": self.url + "?utm=c"}])
        self.start("--policy", str(policy))
        self.assertEqual(self.merge()[0], 0)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["policy"]["remove_query_keys"], ["utm"])
        self.assertEqual(report["counts"]["accepted"], 1)
        members = report["collision_groups"][0]["members"]
        self.assertEqual([m["url"] for m in members], [self.url + "?utm=" + x for x in "abc"])
        self.assertEqual(report["counts"]["changed_keys"], 3)
        # A third batch replays the complete history with stable indices.
        third = self.root / "c.jsonl"
        self.write_batch(third, [{"url": "https://other.test/"}])
        self.assertEqual(self.cli(third, "--checkpoint-in", str(self.next_checkpoint))[0], 0)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["counts"]["accepted"], 2)
        self.assertEqual(report["checkpoint"]["batches"], 3)
        self.assertFalse(report["checkpoint"]["emitted"])

    def test_conflicting_rules_report_both_values_and_preserve_outputs(self):
        self.start()
        saved = self.frontier.read_bytes(), self.checkpoint.read_bytes()
        self.next_checkpoint.write_text("old next checkpoint")
        policy = self.root / "policy.json"
        policy.write_text('{"fragment":"drop","remove_query_keys":["utm"],"allow_ambiguous_merges":true}')
        code, _, err = self.merge("--policy", str(policy))
        self.assertEqual(code, 2)
        self.assertIn("checkpoint dedupe policy conflicts", err)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["status"], "rejected")
        self.assertFalse(report["frontier_emitted"])
        self.assertFalse(report["checkpoint"]["emitted"])
        conflicts = {c["field"]: (c["checkpoint"], c["requested"])
                     for c in report["checkpoint"]["policy_conflicts"]}
        self.assertEqual(conflicts, {"fragment": ("preserve", "drop"), "remove_query_keys": ([], ["utm"]),
                                     "allow_ambiguous_merges": (False, True)})
        self.assertEqual((self.frontier.read_bytes(), self.checkpoint.read_bytes()), saved)
        self.assertEqual(self.next_checkpoint.read_text(), "old next checkpoint")

    def test_cross_batch_policy_created_collision_is_refused(self):
        policy = self.root / "policy.json"
        policy.write_text('{"fragment":"drop"}')
        self.write_batch(self.first, [{"url": self.url + "#a"}])
        self.write_batch(self.second, [{"url": self.url + "#b"}])
        self.start("--policy", str(policy))  # normalization alone has no collision
        saved = self.frontier.read_bytes()
        self.assertEqual(self.merge()[0], 2)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["checkpoint"]["policy_conflicts"], [])
        self.assertEqual(report["counts"]["policy_created_collision_groups"], 1)
        self.assertEqual([m["location"] for m in report["collision_groups"][0]["members"]],
                         ["batch 1 line 1", "batch 2 line 1"])
        self.assertEqual(self.frontier.read_bytes(), saved)
        self.assertFalse(self.next_checkpoint.exists())

    def test_crawlee_then_jsonl_merge_retains_payload_metadata_and_original(self):
        values = [self.url, {"url": self.url, "method": "POST", "payload": "a=1",
                             "userData": {"label": "synthetic"}}]
        self.first.write_text(json.dumps(values))
        self.start("--format", "crawlee")
        self.assertEqual(self.merge()[0], 0)
        report = json.loads(self.report.read_text())
        members = report["collision_groups"][0]["members"]
        self.assertEqual(members[0]["location"], "batch 1 item 2")
        self.assertEqual(members[0]["original"], values[1])
        frontier = [json.loads(line) for line in self.frontier.read_text().splitlines()]
        self.assertEqual(frontier[1]["body"], "a=1")
        self.assertEqual(frontier[1]["metadata"], {"userData": {"label": "synthetic"}})

    def test_skip_is_explicit_on_every_merge_and_old_issues_are_retained(self):
        self.first.write_text(self.first.read_text() + "not JSON\n")
        self.assertEqual(self.cli(self.first, "--checkpoint-out", str(self.checkpoint))[0], 2)
        self.assertFalse(self.checkpoint.exists())
        self.start("--on-invalid", "skip")
        saved = self.frontier.read_bytes()
        self.assertEqual(self.merge()[0], 2)
        self.assertEqual(self.frontier.read_bytes(), saved)
        self.assertFalse(self.next_checkpoint.exists())
        self.assertEqual(self.merge("--on-invalid", "skip")[0], 0)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["counts"]["raw_records"], 5)
        self.assertEqual(report["counts"]["invalid"], 1)
        self.assertEqual(report["issues"][0]["location"], "batch 1 line 3")
        self.assertEqual(report["collision_groups"][0]["members"][1]["index"], 4)

    def test_malformed_checkpoint_is_fatal_before_any_output_write(self):
        self.start()
        good = json.loads(self.checkpoint.read_text())
        bad = [None, {}, {**good, "version": True}, {**good, "version": 3}, {**good, "extra": 1},
               {**good, "policy": {}}, {**good, "batches": []}, {**good, "batches": [None]},
               {**good, "batches": [{"format": "future", "text": ""}]},
               {**good, "batches": [{"format": "jsonl", "text": None}]},
               {**good, "batches": [{"format": "jsonl", "text": "\ud800"}]},
               {**good, "batches": [{"format": "crawlee", "text": "[broken"}]},
               {**good, "batches": [{"format": "jsonl", "text": "", "extra": 1}]}]
        saved = self.frontier.read_bytes(), self.report.read_bytes()
        for value in bad:
            with self.subTest(value=value):
                self.checkpoint.write_text(json.dumps(value))
                self.assertEqual(self.merge()[0], 2)
                self.assertEqual((self.frontier.read_bytes(), self.report.read_bytes()), saved)
                self.assertFalse(self.next_checkpoint.exists())
        self.checkpoint.write_text('{"version":1,"version":1}')
        self.assertEqual(self.merge()[0], 2)
        self.assertEqual((self.frontier.read_bytes(), self.report.read_bytes()), saved)

    def test_deterministic_replay_and_dry_run_never_writes_checkpoint(self):
        self.start()
        saved = self.frontier.read_bytes(), self.report.read_bytes(), self.checkpoint.read_bytes()
        with patch("qawrench.frontier.cli.atomic_write", side_effect=AssertionError("write forbidden")):
            code, out, _ = self.merge("--dry-run")
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["checkpoint"]["emitted"])
        self.assertEqual((self.frontier.read_bytes(), self.report.read_bytes(), self.checkpoint.read_bytes()), saved)
        self.assertFalse(self.next_checkpoint.exists())
        self.assertEqual(self.merge()[0], 0)
        merged = self.frontier.read_bytes(), self.report.read_bytes(), self.next_checkpoint.read_bytes()
        self.assertEqual(self.merge()[0], 0)
        self.assertEqual((self.frontier.read_bytes(), self.report.read_bytes(), self.next_checkpoint.read_bytes()), merged)

    def test_checkpoint_input_output_aliases_are_protected(self):
        self.start()
        saved = self.checkpoint.read_bytes()
        for output in (self.checkpoint, self.second, self.report, self.frontier):
            with self.subTest(output=output):
                self.assertEqual(self.cli(self.second, "--checkpoint-in", str(self.checkpoint),
                                           "--checkpoint-out", str(output))[0], 2)
                self.assertEqual(self.checkpoint.read_bytes(), saved)
        self.next_checkpoint.hardlink_to(self.checkpoint)
        self.assertEqual(self.merge()[0], 2)
        self.next_checkpoint.unlink()
        self.next_checkpoint.symlink_to(self.checkpoint)
        self.assertEqual(self.merge()[0], 2)
        self.next_checkpoint.unlink()
        # Frontier/report may not overwrite the checkpoint input either.
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(self.second), "--checkpoint-in", str(self.checkpoint),
                                   "--frontier", str(self.checkpoint), "--report", str(self.report)]), 2)

    def test_failed_checkpoint_replacement_keeps_old_checkpoint_and_cleans_temp(self):
        self.start()
        self.next_checkpoint.write_text("old checkpoint")
        saved_report = self.report.read_bytes()
        import os
        replace = os.replace

        def fail_checkpoint(source, target):
            if target == self.next_checkpoint:
                raise OSError("synthetic checkpoint disk failure")
            replace(source, target)

        with patch("qawrench.frontier.cli.os.replace", side_effect=fail_checkpoint):
            code, _, err = self.merge()
        self.assertEqual(code, 2)
        self.assertIn("synthetic checkpoint disk failure", err)
        self.assertEqual(self.next_checkpoint.read_text(), "old checkpoint")
        self.assertEqual(self.report.read_bytes(), saved_report)
        self.assertEqual(list(self.root.glob(".b.checkpoint.json.*")), [])
        # Per-file atomicity is explicit: frontier was replaced before this failure.
        self.assertEqual(len(self.frontier.read_text().splitlines()), 3)

    def test_robots_is_reaudited_for_every_original_in_all_batches(self):
        self.start()
        robots = self.root / "robots.json"
        robots.write_text(json.dumps({"version": 1, "origins": {"https://example.test": {
            "status": "available", "source": "synthetic:current", "text": "User-agent: *\nDisallow: /submit"}}}))
        self.assertEqual(self.merge("--robots", str(robots), "--user-agent", "AuditBot")[0], 0)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["robots"]["counts"]["disallow"], 4)
        self.assertEqual(report["counts"]["accepted"], 3)
        self.assertEqual(report["robots"]["origins"][0]["requests"][2]["location"], "batch 2 line 1")


class CheckpointLibraryTests(unittest.TestCase):
    def test_roundtrip_matches_single_combined_audit_and_rule_order_is_irrelevant(self):
        policy = Policy(remove_query_keys=("a", "b"))
        checkpoint = Checkpoint(policy).append('{"url":"https://example.test/","method":"POST","body":"x "}\n')
        checkpoint = Checkpoint.from_dict(strict_json(json.dumps(checkpoint.to_dict())))
        merged = checkpoint.append('{"url":"https://example.test/","method":"POST","body":"x"}\n')
        records, issues, total = merged.load_requests()
        frontier, report = audit(records, merged.policy, issues=issues, total=total)
        self.assertEqual([(r["method"], r["body"]) for r in frontier], [("POST", "x "), ("POST", "x")])
        self.assertEqual(report["status"], "ok")
        self.assertEqual(merged.policy_conflicts(Policy(remove_query_keys=("b", "a"))), [])
        for field, value in (("fragment", "drop"), ("idna", "ascii"), ("w3lib_sort_query", True),
                             ("allow_ambiguous_merges", True)):
            with self.subTest(field=field):
                requested = Policy(remove_query_keys=("a", "b"), **{field: value})
                self.assertEqual(merged.policy_conflicts(requested)[0]["field"], field)


if __name__ == "__main__":
    unittest.main()
