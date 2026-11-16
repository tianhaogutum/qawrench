"""Stable batch IDs identify exact local text snapshots, not request identity."""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qawrench.frontier import AuditError, BatchConflict, Checkpoint, Policy
from qawrench.frontier.cli import main


class BatchIdCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.batch = self.root / "batch.jsonl"
        self.text = '{"url":"https://example.test/submit","method":"POST","body":"a=1"}\n'
        self.batch.write_text(self.text)
        self.frontier = self.root / "frontier.jsonl"
        self.report = self.root / "report.json"
        self.state = self.root / "state.json"
        self.next_state = self.root / "next.json"

    def cli(self, *options):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([str(self.batch), "--frontier", str(self.frontier),
                         "--report", str(self.report), *options])
        return code, out.getvalue(), err.getvalue()

    def start(self):
        self.assertEqual(self.cli("--batch-id", "seed:001", "--checkpoint-out", str(self.state))[0], 0)

    def replay(self, *options):
        return self.cli("--batch-id", "seed:001", "--checkpoint-in", str(self.state),
                        "--checkpoint-out", str(self.next_state), *options)

    def test_identical_id_replay_keeps_history_counts_and_checkpoint_bytes(self):
        self.start()
        saved = self.state.read_bytes(), self.frontier.read_bytes()
        initial = json.loads(self.report.read_text())
        self.assertEqual(initial["checkpoint"]["incoming_batch"], {"batch_id": "seed:001", "status": "appended"})
        self.assertEqual(self.replay()[0], 0)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["checkpoint"]["incoming_batch"], {"batch_id": "seed:001", "status": "replayed"})
        self.assertEqual(report["checkpoint"]["batches"], 1)
        self.assertEqual(report["counts"], initial["counts"])
        self.assertEqual(report["representatives"], initial["representatives"])
        self.assertEqual(report["collision_groups"], initial["collision_groups"])
        self.assertEqual((self.next_state.read_bytes(), self.frontier.read_bytes()), saved)
        self.assertEqual(self.state.read_bytes(), saved[0])

    def test_conflict_reports_both_snapshots_and_only_writes_report(self):
        self.start()
        self.next_state.write_bytes(b"old next checkpoint\n")
        saved = [p.read_bytes() for p in (self.state, self.frontier, self.next_state)]
        variants = [(self.text.replace('a=1', 'a=2'), "jsonl"),
                    (self.text.replace('POST', 'GET'), "jsonl"),
                    (self.text.replace('}', ',"source":"changed"}'), "jsonl"),
                    (self.text + "\n", "jsonl"),
                    (self.text.replace('\n', '\r\n'), "jsonl"),
                    (self.text, "crawlee"), ("[broken", "crawlee"), ("not JSON\n", "jsonl")]
        from qawrench.frontier.cli import atomic_write
        for text, dialect in variants:
            with self.subTest(text=text, dialect=dialect):
                self.batch.write_bytes(text.encode("utf-8"))
                with patch("qawrench.frontier.cli.atomic_write", wraps=atomic_write) as writer, \
                        patch("socket.socket", side_effect=AssertionError("network forbidden")):
                    code, _, err = self.replay("--format", dialect)
                self.assertEqual(code, 2)
                self.assertIn("batch ID conflict: seed:001", err)
                self.assertEqual([call.args[0] for call in writer.call_args_list], [self.report])
                report = json.loads(self.report.read_text())
                self.assertEqual(report["status"], "rejected")
                self.assertFalse(report["frontier_emitted"])
                self.assertFalse(report["checkpoint"]["emitted"])
                self.assertEqual(report["checkpoint"]["incoming_batch"]["status"], "conflict")
                self.assertEqual(report["counts"]["raw_records"], 1)  # saved history only
                self.assertEqual(report["checkpoint"]["batch_conflicts"], [{
                    "batch_id": "seed:001",
                    "checkpoint": {"format": "jsonl", "text_sha256": hashlib.sha256(self.text.encode()).hexdigest()},
                    "requested": {"format": dialect, "text_sha256": hashlib.sha256(text.encode()).hexdigest()}}])
                self.assertEqual([p.read_bytes() for p in (self.state, self.frontier, self.next_state)], saved)

    def test_conflict_preserves_absent_outputs_and_dry_run_never_writes(self):
        self.start()
        self.frontier.unlink()
        for text, expected in ((self.text, 0), (self.text + "\n", 2)):
            self.batch.write_text(text)
            with self.subTest(expected=expected), \
                    patch("qawrench.frontier.cli.atomic_write", side_effect=AssertionError("write forbidden")):
                code, out, _ = self.replay("--dry-run")
            self.assertEqual(code, expected)
            self.assertFalse(json.loads(out)["checkpoint"]["emitted"])
            self.assertFalse(self.frontier.exists())
            self.assertFalse(self.next_state.exists())
        self.assertEqual(self.replay()[0], 2)
        self.assertFalse(self.frontier.exists())
        self.assertFalse(self.next_state.exists())

    def test_legacy_anonymous_history_migrates_and_new_ids_still_append(self):
        self.assertEqual(self.cli("--checkpoint-out", str(self.state))[0], 0)
        self.assertEqual(json.loads(self.state.read_text())["version"], 1)
        self.assertEqual(self.replay()[0], 0)  # anonymous content cannot claim this ID
        state = json.loads(self.next_state.read_text())
        self.assertEqual(state["version"], 2)
        self.assertEqual([b["batch_id"] for b in state["batches"]], [None, "seed:001"])
        self.state.write_bytes(self.next_state.read_bytes())
        # A different ID is an intentional additional occurrence of the same text.
        self.assertEqual(self.cli("--batch-id", "seed:002", "--checkpoint-in", str(self.state),
                                 "--checkpoint-out", str(self.next_state))[0], 0)
        state = json.loads(self.next_state.read_text())
        self.assertEqual([b["batch_id"] for b in state["batches"]], [None, "seed:001", "seed:002"])
        # Replay an older, non-tail batch without losing or reindexing later witnesses.
        self.state.write_bytes(self.next_state.read_bytes())
        before = json.loads(self.report.read_text())
        self.assertEqual(self.replay()[0], 0)
        after = json.loads(self.report.read_text())
        for key in ("counts", "collision_groups", "representatives"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(self.next_state.read_bytes(), self.state.read_bytes())
        # Omission remains anonymous, even alongside named history.
        self.assertEqual(self.cli("--checkpoint-in", str(self.state))[0], 0)
        self.assertEqual(json.loads(self.report.read_text())["counts"]["raw_records"], 4)

    def test_exact_line_endings_are_preserved_for_named_batches(self):
        crlf = self.text.replace("\n", "\r\n")
        self.batch.write_bytes(crlf.encode())
        self.start()
        self.assertEqual(json.loads(self.state.read_text())["batches"][0]["text"], crlf)
        self.assertEqual(self.replay()[0], 0)
        self.batch.write_bytes(self.text.encode())
        self.assertEqual(self.replay()[0], 2)

    def test_preserving_cr_snapshot_does_not_change_previous_line_parsing(self):
        # File loading previously used universal newlines. Keep that parsing
        # behavior while retaining the exact CR text for batch comparison.
        text = self.text.replace("\n", "\r") * 2
        self.batch.write_bytes(text.encode())
        self.start()
        self.assertEqual(json.loads(self.state.read_text())["batches"][0]["text"], text)
        self.assertEqual(json.loads(self.report.read_text())["counts"]["raw_records"], 2)
        self.assertEqual(self.replay()[0], 0)

    def test_bad_ids_and_missing_checkpoint_option_fail_before_any_write(self):
        self.start()
        saved = [p.read_bytes() for p in (self.state, self.frontier, self.report)]
        for batch_id in ("", " ", "seed/001", "é", "a" * 129, "a\nb", "-seed"):
            with self.subTest(batch_id=batch_id):
                self.assertEqual(self.cli("--batch-id=" + batch_id, "--checkpoint-in", str(self.state))[0], 2)
        self.assertEqual(self.cli("--batch-id", "valid")[0], 2)
        self.assertEqual(self.cli("--batch-id", "valid", "--dry-run")[0], 2)
        self.assertEqual([p.read_bytes() for p in (self.state, self.frontier, self.report)], saved)

    def test_tampered_persisted_ids_are_fatal_before_report_write(self):
        self.start()
        state = json.loads(self.state.read_text())
        batch = state["batches"][0]
        variants = [[batch, batch], [batch, {**batch, "text": self.text + "\n"}],
                    [{**batch, "batch_id": False}], [{**batch, "batch_id": ""}],
                    [{"format": batch["format"], "text": batch["text"]}],
                    [{**batch, "extra": True}]]
        saved = self.frontier.read_bytes(), self.report.read_bytes()
        for batches in variants:
            with self.subTest(batches=batches):
                self.state.write_text(json.dumps({**state, "batches": batches}))
                self.assertEqual(self.replay()[0], 2)
                self.assertEqual((self.frontier.read_bytes(), self.report.read_bytes()), saved)
                self.assertFalse(self.next_state.exists())
        self.state.write_text(json.dumps({**state, "version": 1}))  # no IDs allowed in v1
        self.assertEqual(self.replay()[0], 2)
        self.assertEqual((self.frontier.read_bytes(), self.report.read_bytes()), saved)

    def test_replay_still_checks_policy_invalid_history_and_current_robots(self):
        self.batch.write_text(self.text + "not JSON\n")
        self.assertEqual(self.cli("--batch-id", "seed:001", "--on-invalid", "skip",
                                 "--checkpoint-out", str(self.state))[0], 0)
        saved = self.frontier.read_bytes(), self.state.read_bytes()
        self.assertEqual(self.replay()[0], 2)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["counts"]["invalid"], 1)
        self.assertEqual(report["checkpoint"]["incoming_batch"]["status"], "replayed")
        self.assertEqual(self.replay("--on-invalid", "skip")[0], 0)
        self.next_state.unlink()
        policy = self.root / "policy.json"
        policy.write_text('{"fragment":"drop"}')
        self.assertEqual(self.replay("--on-invalid", "skip", "--policy", str(policy))[0], 2)
        self.assertTrue(json.loads(self.report.read_text())["checkpoint"]["policy_conflicts"])
        robots = self.root / "robots.json"
        robots.write_text(json.dumps({"version": 1, "origins": {"https://example.test": {
            "status": "available", "source": "synthetic:current", "text": "User-agent: *\nDisallow: /"}}}))
        self.assertEqual(self.replay("--on-invalid", "skip", "--robots", str(robots),
                                     "--user-agent", "AuditBot", "--enforce-robots")[0], 2)
        report = json.loads(self.report.read_text())
        self.assertEqual(report["robots"]["counts"]["disallow"], 1)
        self.assertEqual(report["checkpoint"]["incoming_batch"]["status"], "replayed")
        self.assertEqual((self.frontier.read_bytes(), self.state.read_bytes()), saved)
        self.assertFalse(self.next_state.exists())


class BatchIdLibraryTests(unittest.TestCase):
    def test_immutable_replay_roundtrip_and_typed_conflict(self):
        text = '["https://example.test/"]'
        state = Checkpoint(Policy()).append(text, "crawlee", batch_id="job:1")
        self.assertIs(state.append(text, "crawlee", batch_id="job:1"), state)
        restored = Checkpoint.from_dict(state.to_dict())
        self.assertEqual(restored.to_dict(), state.to_dict())
        self.assertIs(restored.append(text, "crawlee", batch_id="job:1"), restored)
        with self.assertRaises(BatchConflict) as caught:
            restored.append('[broken', "crawlee", batch_id="job:1")
        self.assertIsInstance(caught.exception, AuditError)
        self.assertEqual(caught.exception.conflict["batch_id"], "job:1")
        self.assertEqual(len(state.batches), 1)
        self.assertEqual(len(state.append(text, "crawlee", batch_id="JOB:1").batches), 2)
        self.assertEqual(len(state.append(text, "crawlee").batches), 2)
        self.assertEqual(state.append(text, "crawlee", batch_id="a" * 128).version, 2)

    def test_direct_construction_and_append_do_not_accept_duplicate_or_bad_ids(self):
        state = Checkpoint(Policy()).append('', batch_id="empty:1")
        with self.assertRaises(AuditError):
            Checkpoint(Policy(), state.batches * 2)
        for value in (True, 1, [], {}, "", "ü", "a" * 129):
            with self.subTest(value=value), self.assertRaises(AuditError):
                state.append('', batch_id=value)
        self.assertIs(state.append('', batch_id="empty:1"), state)


if __name__ == "__main__":
    unittest.main()
