"""Inventory contracts: discover explicit audit inputs without opening them."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from qawrench.flaky import import_report, prune

ROOT = Path(__file__).resolve().parents[2]
PW = (ROOT / "examples/flaky/playwright.json").read_bytes()
CTRF = (ROOT / "examples/flaky/ctrf.json").read_bytes()


class ArchiveInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "history.sqlite"
        import_report(self.db, PW, format="playwright", run_id="01-pw")
        import_report(self.db, CTRF, format="ctrf", project="chromium", run_id="02-ctrf")
        empty = json.loads(PW)
        empty["suites"] = []
        for key in ("expected", "unexpected", "flaky", "skipped"):
            empty["stats"][key] = 0
        import_report(self.db, json.dumps(empty).encode(), format="playwright", run_id="03-empty")
        import_report(self.db, PW, format="playwright", run_id="04-full")
        self.execute("UPDATE runs SET imported_at='2026-01-01'")
        self.first = self.root / "first.json"
        self.second = self.root / "second.json"
        prune(self.db, keep_latest=2, archive_path=self.first)
        prune(self.db, keep_latest=1, archive_path=self.second)
        self.expected = {"action": "listed", "archives": sorted([
            {"archive_sha256": hashlib.sha256(self.first.read_bytes()).hexdigest(),
             "paths": [str(self.first)], "run_ids": ["01-pw", "02-ctrf"], "observations": 14, "attempts": 16},
            {"archive_sha256": hashlib.sha256(self.second.read_bytes()).hexdigest(),
             "paths": [str(self.second)], "run_ids": ["03-empty"], "observations": 0, "attempts": 0},
        ], key=lambda item: item["archive_sha256"])}

    def execute(self, sql, parameters=()):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute(sql, parameters)

    def inventory(self):
        from qawrench.flaky import list_archives
        return list_archives(self.db)

    def cli(self, expected=0):
        result = subprocess.run([sys.executable, "-m", "qawrench.flaky", "list-archives",
                                 "--db", str(self.db)], cwd=self.root,
                                env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, expected, result.stderr.decode())
        self.assertNotIn(b"Traceback", result.stderr)
        return result

    def reject(self, diagnostic):
        before = self.db.read_bytes() if self.db.exists() else None
        result = self.cli(expected=2)
        self.assertEqual(result.stdout, b"")
        self.assertIn(diagnostic.encode(), result.stderr)
        self.assertEqual(self.db.read_bytes() if self.db.exists() else None, before)

    def test_cli_multi_prune_contract_is_deterministic_and_read_only(self):
        before = self.db.read_bytes(), self.first.read_bytes(), self.second.read_bytes()
        result = self.cli()
        self.assertEqual(json.loads(result.stdout), self.expected)
        self.assertEqual(result.stderr, b"")
        self.assertEqual(self.inventory(), self.expected)
        self.assertEqual(self.cli().stdout, result.stdout)
        self.assertEqual((self.db.read_bytes(), self.first.read_bytes(), self.second.read_bytes()), before)

    def test_paths_are_informational_sorted_and_never_opened(self):
        self.first.unlink()
        self.second.write_bytes(b"unrelated corrupt bytes are not audited")
        with closing(sqlite3.connect(self.db)) as db:
            ref = json.loads(db.execute("SELECT archive FROM runs WHERE run_id='01-pw'").fetchone()[0])
        self.execute("UPDATE runs SET archive=? WHERE run_id='01-pw'",
                     (json.dumps({**ref, "path": "../missing/ü.json"}),))
        expected = json.loads(json.dumps(self.expected))
        next(a for a in expected["archives"] if a["run_ids"] == ["01-pw", "02-ctrf"])["paths"] = sorted([
            "../missing/ü.json", str(self.first)])
        # SQLite opens only its explicitly supplied database via its C API.
        with patch("pathlib.Path.open", side_effect=AssertionError("external file opened")), \
                patch("builtins.open", side_effect=AssertionError("external file opened")):
            self.assertEqual(self.inventory(), expected)
        self.assertFalse(self.first.exists())
        self.assertEqual(self.second.read_bytes(), b"unrelated corrupt bytes are not audited")

    def test_invalid_references_fail_before_stdout_without_writes(self):
        with closing(sqlite3.connect(self.db)) as db:
            ref = json.loads(db.execute("SELECT archive FROM runs WHERE run_id='01-pw'").fetchone()[0])
        baseline = self.db.read_bytes()
        values = ["{", "[]", '{"path":"a","path":"b"}', json.dumps({**ref, "extra": 1}),
                  json.dumps({key: value for key, value in ref.items() if key != "path"})]
        for key, value in (("path", " "), ("path", 1), ("sha256", "bad"),
                           ("sha256", ref["sha256"].upper()), ("source_sha256", "g" * 64),
                           ("source_sha256", "0" * 64)):
            values.append(json.dumps({**ref, key: value}))
        for value in values:
            with self.subTest(reference=value):
                self.db.write_bytes(baseline)
                self.execute("UPDATE runs SET archive=? WHERE run_id='01-pw'", (value,))
                self.reject("cannot list archives")
        self.db.write_bytes(baseline)
        self.execute("UPDATE runs SET digest='wrong' WHERE run_id='01-pw'")
        self.reject("source SHA-256")

    def test_missing_malformed_and_invalid_retained_counts_fail_closed(self):
        with closing(sqlite3.connect(self.db)) as db:
            test_id, text = db.execute("SELECT test_id,data FROM observations WHERE run_id='01-pw' LIMIT 1").fetchone()
        entry = json.loads(text)
        values = ["{", "[]", json.dumps({key: value for key, value in entry.items() if key != "retained_attempts"}),
                  json.dumps({**entry, "retained_attempts": []}),
                  json.dumps({**entry, "retained_attempts": {}})]
        for count in (None, True, -1, 1.0, "1", 2**53, float("inf")):
            values.append(json.dumps({**entry, "retained_attempts": {**entry["retained_attempts"], "available_attempts": count}}))
        baseline = self.db.read_bytes()
        for value in values:
            with self.subTest(data=value):
                self.db.write_bytes(baseline)
                self.execute("UPDATE observations SET data=? WHERE run_id='01-pw' AND test_id=?", (value, test_id))
                self.reject("cannot list archives")

    def test_unpruned_v1_and_v2_ignore_full_run_evidence(self):
        self.execute("UPDATE runs SET metadata='corrupt' WHERE run_id='04-full'")
        self.execute("UPDATE observations SET data='corrupt' WHERE run_id='04-full'")
        self.assertEqual(self.inventory(), self.expected)
        self.db = self.root / "v1.sqlite"
        import_report(self.db, PW, format="playwright", run_id="full")
        before = self.db.read_bytes()
        self.assertEqual(json.loads(self.cli().stdout), {"action": "listed", "archives": []})
        self.assertEqual(self.db.read_bytes(), before)
        self.db = self.root / "missing.sqlite"
        self.reject("cannot read ledger")
        self.db = self.root / "foreign.sqlite"
        self.execute("CREATE TABLE other (x)")
        self.reject("not a retry-history-ledger")

    def test_inventory_counts_and_references_share_one_snapshot(self):
        from qawrench.flaky.ledger import _read_db
        # A writer changes the committed counts after the reader pins its view.
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            def concurrent_reader(path):
                db = _read_db(path)
                with writer:
                    writer.execute("DELETE FROM observations WHERE run_id='01-pw'")
                return db
            with patch("qawrench.flaky.verification._read_db", side_effect=concurrent_reader) as read:
                self.assertEqual(self.inventory(), self.expected)
                read.assert_called_once_with(self.db)
