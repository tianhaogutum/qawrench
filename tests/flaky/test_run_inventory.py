"""Discover exact audit targets without replaying or opening stored evidence."""

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

from qawrench.flaky import import_ledger, import_report, prune

ROOT = Path(__file__).resolve().parents[2]
PW = (ROOT / "examples/flaky/playwright.json").read_bytes()
CTRF = (ROOT / "examples/flaky/ctrf.json").read_bytes()


class RunInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "history.sqlite"
        empty = json.loads(PW)
        empty["suites"] = []
        for key in ("expected", "unexpected", "flaky", "skipped"):
            empty["stats"][key] = 0
        self.empty = json.dumps(empty).encode()
        for run_id, raw, format, project in (
                ("01-pw", PW, "playwright", None), ("02-ctrf", CTRF, "ctrf", "chromium"),
                ("03-empty", self.empty, "playwright", None), ("04-full", PW, "playwright", None),
                ("05-empty-full", self.empty, "playwright", None)):
            import_report(self.db, raw, format=format, run_id=run_id, project=project)
        self.execute("UPDATE runs SET imported_at='2026-01-01'")
        self.first, self.second = self.root / "first.json", self.root / "second.json"
        prune(self.db, keep_latest=3, archive_path=self.first)
        prune(self.db, keep_latest=2, archive_path=self.second)
        self.expected = {"action": "listed", "runs": []}
        for run_id, raw, format, project, observations, attempts, archive in (
                ("01-pw", PW, "playwright", None, 8, 9, self.first),
                ("02-ctrf", CTRF, "ctrf", "chromium", 6, 7, self.first),
                ("03-empty", self.empty, "playwright", None, 0, 0, self.second),
                ("04-full", PW, "playwright", None, 8, 9, None),
                ("05-empty-full", self.empty, "playwright", None, 0, 0, None)):
            row = dict(run_id=run_id, format=format, project_context=project,
                       state="archived" if archive else "full", source_sha256=hashlib.sha256(raw).hexdigest(),
                       observations=observations, attempts=attempts)
            if archive:
                row["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.expected["runs"].append(row)

    def execute(self, sql, parameters=()):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute(sql, parameters)

    def inventory(self, state="all"):
        from qawrench.flaky import list_runs
        return list_runs(self.db, state=state)

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, "-m", "qawrench.flaky", "list-runs",
                                 "--db", str(self.db), *args], cwd=self.root,
                                env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, expected, result.stderr.decode())
        self.assertNotIn(b"Traceback", result.stderr)
        return result

    def reject(self, diagnostic, *args):
        before = self.db.read_bytes() if self.db.exists() else None
        result = self.cli(*args, expected=2)
        self.assertEqual(result.stdout, b"")
        self.assertIn(diagnostic.encode(), result.stderr)
        self.assertEqual(self.db.read_bytes() if self.db.exists() else None, before)

    def test_cli_mixed_multi_prune_contract_filters_and_read_only(self):
        before = self.db.read_bytes(), self.first.read_bytes(), self.second.read_bytes()
        result = self.cli()
        self.assertEqual(json.loads(result.stdout), self.expected)
        self.assertEqual(result.stderr, b"")
        self.assertEqual(self.cli("--state", "all").stdout, result.stdout)
        for state in ("full", "archived"):
            expected = {"action": "listed", "runs": [r for r in self.expected["runs"] if r["state"] == state]}
            self.assertEqual(json.loads(self.cli("--state", state).stdout), expected)
        self.assertEqual((self.db.read_bytes(), self.first.read_bytes(), self.second.read_bytes()), before)

    def test_v1_empty_and_exact_case_sensitive_ids(self):
        self.db = self.root / "v1.sqlite"
        import_ledger(self.db, b'{"ledger_format":"retry-history-ledger","version":1,"runs":[]}')
        self.assertEqual(json.loads(self.cli().stdout), {"action": "listed", "runs": []})
        for run_id, raw, format, project in (("same", PW, "playwright", None),
                                           ("Same", CTRF, "ctrf", "chromium"),
                                           (" same ", self.empty, "playwright", None)):
            import_report(self.db, raw, format=format, project=project, run_id=run_id)
        before = self.db.read_bytes()
        rows = json.loads(self.cli().stdout)["runs"]
        self.assertEqual([r["run_id"] for r in rows], [" same ", "Same", "same"])
        self.assertEqual([(r["observations"], r["attempts"]) for r in rows], [(0, 0), (6, 7), (8, 9)])
        self.assertTrue(all(r["state"] == "full" and "archive_sha256" not in r for r in rows))
        self.assertEqual(json.loads(self.cli("--state", "archived").stdout), {"action": "listed", "runs": []})
        self.assertEqual(self.db.read_bytes(), before)

    def test_selected_identity_context_and_reference_errors_fail_closed(self):
        baseline = self.db.read_bytes()
        for column, value, diagnostic in (
                ("run_id", " ", "stored run-id"), ("format", "unknown", "format"),
                ("format", "CTRF", "format"), ("project_context", "chromium", "project context"),
                ("digest", "bad", "source SHA-256"), ("digest", "A" * 64, "source SHA-256"),
                ("archive", "{", "retained JSON"), ("archive", "[]", "archive reference"),
                ("archive", '{"path":"a","path":"b"}', "duplicate object key"),
                ("archive", json.dumps({"path":" ", "sha256":"0" * 64, "source_sha256":hashlib.sha256(PW).hexdigest()}), "archive path"),
                ("archive", json.dumps({"path":"opaque", "sha256":"z" * 64, "source_sha256":hashlib.sha256(PW).hexdigest()}), "sha256"),
                ("archive", json.dumps({"path":"opaque", "sha256":"0" * 64, "source_sha256":"0" * 64}), "source SHA-256")):
            with self.subTest(column=column, value=value):
                self.db.write_bytes(baseline)
                self.execute(f"UPDATE runs SET {column}=? WHERE run_id='01-pw'", (value,))
                self.reject(diagnostic)
        self.db.write_bytes(baseline)
        self.execute("UPDATE runs SET project_context=' ' WHERE run_id='02-ctrf'")
        self.reject("project context")

    def test_invalid_retained_counts_fail_before_stdout(self):
        with closing(sqlite3.connect(self.db)) as db:
            test_id, text = db.execute("SELECT test_id,data FROM observations WHERE run_id='02-ctrf' LIMIT 1").fetchone()
        entry = json.loads(text)
        baseline = self.db.read_bytes()
        values = ["{", "[]", json.dumps({**entry, "retained_attempts": []}),
                  json.dumps({**entry, "retained_attempts": {}})]
        for count in (None, True, -1, 1.0, "1", 2**53, float("inf")):
            values.append(json.dumps({**entry, "retained_attempts": {"available_attempts": count}}))
        for value in values:
            with self.subTest(value=value):
                self.db.write_bytes(baseline)
                self.execute("UPDATE observations SET data=? WHERE run_id='02-ctrf' AND test_id=?", (value, test_id))
                self.reject("cannot list runs")

    def test_filters_ignore_unselected_evidence_and_do_not_replay_or_open_paths(self):
        self.first.unlink()
        self.second.write_bytes(b"not an archive")
        self.execute("UPDATE runs SET raw_report=?,metadata='{'", (b"not a report",))
        self.execute("UPDATE observations SET data='{' WHERE run_id='04-full'")
        self.execute("UPDATE attempts SET data='{' WHERE run_id='04-full'")
        with patch("pathlib.Path.open", side_effect=AssertionError("external file opened")), \
                patch("builtins.open", side_effect=AssertionError("external file opened")), \
                patch("qawrench.flaky.verification.parse_report", side_effect=AssertionError("replay")):
            self.assertEqual(self.inventory(), self.expected)
        with closing(sqlite3.connect(self.db)) as db:
            original = db.execute("SELECT archive FROM runs WHERE run_id='01-pw'").fetchone()[0]
        self.execute("UPDATE runs SET archive='{' WHERE run_id='01-pw'")
        self.assertEqual(self.inventory("full"), {"action": "listed", "runs": self.expected["runs"][3:]})
        self.execute("UPDATE runs SET archive=? WHERE run_id='01-pw'", (original,))
        self.execute("UPDATE runs SET digest='bad',format='unknown' WHERE run_id='04-full'")
        self.assertEqual(self.inventory("archived"), {"action": "listed", "runs": self.expected["runs"][:3]})
        self.assertFalse(self.first.exists())
        self.assertEqual(self.second.read_bytes(), b"not an archive")

    def test_invalid_state_missing_and_foreign_database(self):
        from qawrench.flaky import LedgerError
        for state in ("", "FULL", None, [], 1):
            with self.subTest(state=state), patch("qawrench.flaky.verification._read_db") as read:
                with self.assertRaisesRegex(LedgerError, "state"):
                    self.inventory(state)
                read.assert_not_called()
        self.reject("invalid choice", "--state", "FULL")
        self.db = self.root / "missing.sqlite"
        self.reject("cannot read ledger")
        self.db = self.root / "foreign.sqlite"
        self.execute("CREATE TABLE other (x)")
        self.reject("not a retry-history-ledger")

    def test_counts_context_and_references_share_one_snapshot(self):
        from qawrench.flaky.ledger import _read_db
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            def concurrent_reader(path):
                db = _read_db(path)
                with writer:
                    writer.execute("DELETE FROM observations")
                    writer.execute("DELETE FROM attempts")
                    writer.execute("UPDATE runs SET digest='bad',archive='{}'")
                return db
            with patch("qawrench.flaky.verification._read_db", side_effect=concurrent_reader) as read:
                self.assertEqual(self.inventory(), self.expected)
                read.assert_called_once_with(self.db)
