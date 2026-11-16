"""Opt-in per-snapshot UTF-8 byte limits validate input before any audit/write."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qawrench.frontier import AuditError, Checkpoint, Policy, RobotsPolicy
from qawrench.frontier.cli import main


class RobotsSizeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "requests.jsonl"
        self.input.write_text('{"url":"https://example.test/private"}\n')
        self.manifest = self.root / "robots.json"
        self.frontier = self.root / "frontier.jsonl"
        self.report = self.root / "report.json"
        self.state = self.root / "next.json"
        self.outputs = (self.frontier, self.report, self.state)
        self.options = ["--robots", str(self.manifest), "--user-agent", "AuditBot"]
        self.snapshot("User-agent: AuditBot\nAllow: /\n")

    def snapshot(self, text, extra=None):
        self.data = {"version": 1, "origins": {
            "https://EXAMPLE.test:443/": {"status": "available", "source": "synthetic:bytes",
                                          "text": text}, **(extra or {})}}
        self.manifest.write_text(json.dumps(self.data), encoding="utf-8")

    def cli(self, *options, history=False):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main([*([] if history else [str(self.input)]),
                             "--frontier", str(self.frontier), "--report", str(self.report),
                             "--checkpoint-out", str(self.state), *options])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_oversized_snapshot_rejected_with_provenance(self):
        size = len(self.data["origins"]["https://EXAMPLE.test:443/"]["text"].encode("utf-8"))
        with self.assertRaises(AuditError) as caught:
            RobotsPolicy.from_dict(self.data, max_robots_bytes=size - 1)
        message = str(caught.exception)
        for witness in ("https://example.test", "synthetic:bytes", f"actual_bytes={size}",
                        f"limit_bytes={size - 1}"):
            self.assertIn(witness, message)

    def test_exact_byte_boundaries_include_unicode_bom_comments_and_line_endings(self):
        for text in ("", "# ASCII\n", "User-agent: AuditBot\nAllow: /café\n",
                     "\ufeffUser-agent: AuditBot\r\n# 雪\rAllow: /\n"):
            size = len(text.encode("utf-8"))
            with self.subTest(text=text, size=size):
                self.snapshot(text)
                expected = RobotsPolicy.from_dict(self.data)
                self.assertEqual(RobotsPolicy.from_dict(self.data, max_robots_bytes=max(1, size)), expected)
                if size:
                    with self.assertRaises(AuditError):
                        RobotsPolicy.from_dict(self.data, max_robots_bytes=size - 1)
                code, out, err = self.cli(*self.options, "--max-robots-bytes", str(max(1, size)), "--dry-run")
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)["status"], "ok")
                if size > 1:
                    code, out, err = self.cli(*self.options, "--max-robots-bytes", str(size - 1), "--dry-run")
                    self.assertEqual(code, 2)
                    self.assertEqual(out, "")
                    self.assertIn(f"actual_bytes={size}", err)

    def test_limit_is_per_available_snapshot_including_unused_origins(self):
        self.snapshot("", {"https://unused.test": {"status": "available", "source": "unused",
                                                    "text": "#雪"}})
        with self.assertRaisesRegex(AuditError, "https://unused.test.*unused.*actual_bytes=4.*limit_bytes=3"):
            RobotsPolicy.from_dict(self.data, max_robots_bytes=3)
        self.snapshot("#雪", {"https://unused.test": {"status": "available", "source": "unused", "text": "#雪"},
                              "https://missing.test": {"status": "unavailable", "source": "s" * 100,
                                                       "reason": "r" * 100}})
        self.assertEqual(self.cli(*self.options, "--max-robots-bytes", "4", "--dry-run")[0], 0)
        RobotsPolicy.from_dict(self.data, max_robots_bytes=4)  # not the sum, nor source/reason size

    def test_invalid_limits_and_missing_manifest_fail_before_reads_or_writes(self):
        for value in (True, False, 0, -1, 1.5, "10"):
            with self.subTest(value=value), self.assertRaisesRegex(AuditError, "positive integer"):
                RobotsPolicy.from_dict(self.data, max_robots_bytes=value)
        with patch("pathlib.Path.read_text", side_effect=AssertionError("read forbidden")), \
                patch("pathlib.Path.read_bytes", side_effect=AssertionError("read forbidden")), \
                patch("qawrench.frontier.cli.atomic_write", side_effect=AssertionError("write forbidden")):
            for value in ("0", "-1", "1.5", "bad"):
                with self.subTest(value=value):
                    self.assertEqual(self.cli(*self.options, "--max-robots-bytes", value)[0], 2)
            code, _, err = self.cli("--max-robots-bytes", "10")
            self.assertEqual(code, 2)
            self.assertIn("--max-robots-bytes requires --robots", err)

    def test_over_limit_rejection_preserves_all_outputs_before_rule_parsing(self):
        for existing in (False, True):
            if existing:
                for path in self.outputs:
                    path.write_bytes(b"original\n")
            before = {p.name: p.read_bytes() for p in self.root.iterdir()}
            with self.subTest(existing=existing), \
                    patch("qawrench.frontier.robots._parse", side_effect=AssertionError("parse forbidden")), \
                    patch("qawrench.frontier.cli.atomic_write", side_effect=AssertionError("write forbidden")):
                code, out, err = self.cli(*self.options, "--max-robots-bytes", "1")
            self.assertEqual(code, 2)
            self.assertEqual(out, "")
            self.assertIn("limit_bytes=1", err)
            self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)

    def test_accepted_and_rejected_dry_runs_never_write(self):
        for existing in (False, True):
            if existing:
                for path in self.outputs:
                    path.write_bytes(b"original\n")
            for limit, expected in (("1000", 0), ("1", 2)):
                before = {p.name: p.read_bytes() for p in self.root.iterdir()}
                with self.subTest(existing=existing, limit=limit), \
                        patch("qawrench.frontier.cli.atomic_write", side_effect=AssertionError("write forbidden")):
                    self.assertEqual(self.cli(*self.options, "--max-robots-bytes", limit, "--dry-run")[0], expected)
                self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)

    def test_omission_and_accepted_limit_preserve_reports_in_all_robots_modes(self):
        self.snapshot("User-agent: AuditBot\nDisallow /private\nSitemap: https://example.test/map\n")
        for mode in ([], ["--enforce-robots"], ["--enforce-robots", "--strict-robots"]):
            with self.subTest(mode=mode):
                baseline = self.cli(*self.options, *mode, "--dry-run")
                capped = self.cli(*self.options, *mode, "--max-robots-bytes", "1000", "--dry-run")
                self.assertEqual(capped, baseline)
                self.assertEqual(capped[0], 2 if "--strict-robots" in mode else 0)
        self.assertEqual(RobotsPolicy.from_dict(self.data, max_robots_bytes=None),
                         RobotsPolicy.from_dict(self.data))

    def test_checkpoint_only_limit_and_existing_rejection_gates(self):
        self.input.write_text('{"url":"https://example.test/private#a"}\n'
                              '{"url":"https://example.test/private#b"}\n'
                              '{"url":"https://unknown.test/"}\nbroken JSON\n')
        saved = self.root / "saved.json"
        saved.write_text(json.dumps(Checkpoint(Policy()).append(self.input.read_text(), "jsonl").to_dict()))
        policy = self.root / "policy.json"
        policy.write_text('{"fragment":"drop"}')
        self.snapshot("User-agent: AuditBot\nDisallow: /\nAllow no-colon\nDisallow: bad\n")
        options = [*self.options, "--checkpoint-only", "--checkpoint-in", str(saved),
                   "--policy", str(policy), "--enforce-robots", "--strict-robots"]
        code, _, err = self.cli(*options, "--max-robots-bytes", "1000", history=True)
        self.assertEqual(code, 2, err)
        report = json.loads(self.report.read_text())
        self.assertEqual(len(report["errors"]), 5)  # invalid, ambiguity, strict, decisions, saved policy
        self.assertEqual(report["robots"]["counts"], {"allow": 0, "disallow": 2, "unknown": 1})
        self.assertEqual(report["checkpoint"]["incoming_batch"]["status"], "not_supplied")
        self.assertFalse(self.frontier.exists())
        self.assertFalse(self.state.exists())
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        self.assertEqual(self.cli(*options, "--max-robots-bytes", "1", history=True)[0], 2)
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()
