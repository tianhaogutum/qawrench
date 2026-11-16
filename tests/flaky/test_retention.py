"""Archive/prune synthetic histories while preserving aggregate evidence."""

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

from qawrench.flaky import (LedgerError, export_ledger, import_ledger,
                                  import_report, prune, report)

ROOT = Path(__file__).resolve().parents[2]
PW = (ROOT / "examples/flaky/playwright.json").read_bytes()
CTRF = (ROOT / "examples/flaky/ctrf.json").read_bytes()


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "history.sqlite"
        self.archive = self.root / "old-runs.json"
        import_report(self.db, PW, format="playwright", run_id="old-pw")
        import_report(self.db, CTRF, format="ctrf", project="chromium", run_id="old-ctrf")
        import_report(self.db, PW, format="playwright", run_id="recent")
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE runs SET imported_at=CASE run_id WHEN 'old-pw' THEN '2026-01-01 00:00:00' "
                       "WHEN 'old-ctrf' THEN '2026-02-01 00:00:00' ELSE '2026-03-01 00:00:00' END")

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, "-m", "qawrench.flaky", *map(str, args)],
                                cwd=self.root, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, expected, result.stderr.decode())
        self.assertNotIn(b"Traceback", result.stderr)
        return result

    def prune(self, *extra, expected=0):
        return self.cli("prune", "--db", self.db, "--keep-latest", "1", "--archive", self.archive,
                        *extra, expected=expected)

    def test_cli_dry_run_then_prune_preserves_statistics_and_exports_exact_sources(self):
        original = report(self.db)
        before = self.db.read_bytes()
        plan = json.loads(self.prune("--dry-run").stdout)
        self.assertEqual(plan["action"], "planned")
        self.assertEqual(plan["run_ids"], ["old-ctrf", "old-pw"])
        self.assertEqual((plan["observations"], plan["attempts"]), (14, 16))
        self.assertFalse(self.archive.exists())
        self.assertEqual(self.db.read_bytes(), before)

        done = json.loads(self.prune().stdout)
        self.assertEqual(done["action"], "pruned")
        self.assertEqual(done["archive_sha256"], hashlib.sha256(self.archive.read_bytes()).hexdigest())
        after = report(self.db)
        self.assertEqual(after["totals"], original["totals"])
        self.assertEqual(after["tests"], original["tests"])
        self.assertEqual(after["run_count"], 3)
        for previous, current in zip(original["runs"], after["runs"]):
            self.assertEqual(previous["totals"], current["totals"])
            if current["run_id"] == "recent":
                self.assertEqual(previous, current)
                continue
            self.assertEqual(current["archive"]["path"], str(self.archive))
            self.assertEqual(current["archive"]["sha256"], done["archive_sha256"])
            source = CTRF if current["format"] == "ctrf" else PW
            self.assertEqual(current["archive"]["source_sha256"], hashlib.sha256(source).hexdigest())
            for obs in current["observations"]:
                old = next(o for o in previous["observations"] if o["test_id"] == obs["test_id"])
                self.assertTrue(obs["attempts_archived"])
                self.assertEqual(obs["attempts"], [])
                self.assertEqual(obs["available_attempt_count"], len(old["attempts"]))
                for key in ("fail_then_pass", "mapped_fail_then_pass", "terminal_status",
                            "available_attempt_duration_ms", "attempt_details_unavailable"):
                    self.assertEqual(obs[key], old[key])
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM attempts").fetchone()[0], 9)
            self.assertEqual(db.execute("SELECT raw_report FROM runs WHERE run_id='old-pw'").fetchone()[0], b"")
            for (data,) in db.execute("SELECT data FROM observations WHERE run_id != 'recent'"):
                self.assertNotIn("source", json.loads(data))
        restored = self.root / "restored.sqlite"
        self.cli("import-ledger", "--db", restored, self.archive)
        self.assertEqual(self.cli("export", "--db", restored, "--run-id", "old-pw").stdout, PW)
        self.assertEqual(self.cli("export", "--db", restored, "--run-id", "old-ctrf").stdout, CTRF)
        self.assertEqual(report(restored)["totals"]["available_attempts"], 16)
        self.assertIn(b"archived", self.cli("export", "--db", self.db, "--run-id", "old-pw", expected=2).stderr)
        self.assertIn(b"archived", self.cli("export-ledger", "--db", self.db, expected=2).stderr)
        before = self.db.read_bytes()
        self.assertEqual(import_report(self.db, PW, format="playwright", run_id="old-pw")["action"], "unchanged")
        self.assertEqual(json.loads(self.prune().stdout)["action"], "unchanged")
        self.assertEqual(self.db.read_bytes(), before)

    def test_selection_exports_full_runs_from_pruned_ledger_and_rejects_archived_ids(self):
        expected = export_ledger(self.db, run_ids=["recent"])
        prune(self.db, keep_latest=1, archive_path=self.archive)
        before = self.db.read_bytes()
        args = ("export-ledger", "--db", self.db)
        selected = self.cli(*args, "--run-id", "recent", "--run-id", "recent")
        self.assertEqual(selected.stdout, expected)
        restored = self.root / "selected.sqlite"
        import_ledger(restored, selected.stdout)
        self.assertEqual(export_ledger(restored), expected)
        for ids, diagnostic in ((("recent", "old-pw"), b"archived"),
                                (("old-ctrf",), b"archived"),
                                (("recent", "missing"), b"unknown run-id"),
                                (("old-pw", "missing"), b"unknown run-id")):
            with self.subTest(ids=ids):
                flags = [part for run_id in ids for part in ("--run-id", run_id)]
                failed = self.cli(*args, *flags, expected=2)
                self.assertEqual(failed.stdout, b"")
                self.assertIn(diagnostic, failed.stderr)
        self.assertEqual(json.loads(export_ledger(self.db, run_ids=[]))["runs"], [])
        failed = self.cli(*args, expected=2)
        self.assertEqual(failed.stdout, b"")
        self.assertIn(str(self.archive).encode(), failed.stderr)
        self.assertEqual(self.db.read_bytes(), before)

    def test_failed_archive_publication_never_changes_database_or_overwrites_files(self):
        before = self.db.read_bytes()
        self.archive.write_bytes(b"do not overwrite")
        with self.assertRaisesRegex(LedgerError, "File exists"):
            prune(self.db, keep_latest=1, archive_path=self.archive)
        self.assertEqual(self.archive.read_bytes(), b"do not overwrite")
        self.assertEqual(self.db.read_bytes(), before)
        self.archive.unlink()
        with patch("qawrench.flaky.retention.os.fsync", side_effect=OSError("synthetic disk sync failure")):
            with self.assertRaisesRegex(LedgerError, "synthetic disk sync failure"):
                prune(self.db, keep_latest=1, archive_path=self.archive)
        self.assertFalse(self.archive.exists())
        self.assertEqual(list(self.root.glob(".retry-history-*.tmp")), [])
        self.assertEqual(self.db.read_bytes(), before)
        create_temporary = tempfile.NamedTemporaryFile

        def partial_write(*args, **kwargs):
            stream = create_temporary(*args, **kwargs)
            write = stream.write

            def fail(raw):
                write(raw[:10])
                raise OSError("synthetic partial archive write")

            stream.write = fail
            return stream

        with patch("qawrench.flaky.retention.tempfile.NamedTemporaryFile", side_effect=partial_write):
            with self.assertRaisesRegex(LedgerError, "synthetic partial archive write"):
                prune(self.db, keep_latest=1, archive_path=self.archive)
        self.assertFalse(self.archive.exists())
        self.assertEqual(list(self.root.glob(".retry-history-*.tmp")), [])
        self.assertEqual(self.db.read_bytes(), before)
        with self.assertRaisesRegex(LedgerError, "No such file"):
            prune(self.db, keep_latest=1, archive_path=self.root / "missing" / "archive.json")
        self.assertEqual(self.db.read_bytes(), before)

    def test_sql_failure_after_first_pruned_run_rolls_back_schema_and_all_rows(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("CREATE TRIGGER fail_second BEFORE DELETE ON attempts WHEN OLD.run_id='old-pw' "
                       "BEGIN SELECT RAISE(ABORT, 'synthetic late prune failure'); END")
        before = self.db.read_bytes()
        original = report(self.db)
        with self.assertRaisesRegex(LedgerError, "rolled back.*synthetic late prune failure"):
            prune(self.db, keep_latest=1, archive_path=self.archive)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(report(self.db), original)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertNotIn("archive", [row[1] for row in db.execute("PRAGMA table_info(runs)")])
        # A successfully published archive remains usable even if SQL rolls back.
        restored = self.root / "recovered.sqlite"
        import_ledger(restored, self.archive.read_bytes())
        self.assertEqual(report(restored)["totals"]["available_attempts"], 16)
        self.assertEqual(list(self.root.glob(".retry-history-*.tmp")), [])

    def test_failed_directory_sync_leaves_complete_archive_but_database_unchanged(self):
        before = self.db.read_bytes()
        with patch("qawrench.flaky.retention.os.fsync", side_effect=[None, OSError("directory sync failed")]):
            with self.assertRaisesRegex(LedgerError, "directory sync failed"):
                prune(self.db, keep_latest=1, archive_path=self.archive)
        self.assertEqual(self.db.read_bytes(), before)
        import_ledger(self.root / "recovered.sqlite", self.archive.read_bytes())
        self.assertEqual(list(self.root.glob(".retry-history-*.tmp")), [])

    def test_dry_run_is_read_only_and_does_not_probe_archive_writability(self):
        before = self.db.read_bytes()
        result = prune(self.db, keep_latest=1, archive_path=self.root / "missing" / "out.json", dry_run=True)
        self.assertEqual(result["action"], "planned")
        self.assertEqual(self.db.read_bytes(), before)
        missing = self.root / "missing.sqlite"
        for dry_run in (True, False):
            with self.assertRaises(LedgerError):
                prune(missing, keep_latest=0, archive_path=self.archive, dry_run=dry_run)
            self.assertFalse(missing.exists())
        self.assertFalse(self.archive.exists())

    def test_invalid_counts_and_database_sidecars_are_rejected_without_writes(self):
        before = self.db.read_bytes()
        for count in (-1, True, 1.5, "1"):
            with self.subTest(count=count):
                with self.assertRaisesRegex(LedgerError, "nonnegative integer"):
                    prune(self.db, keep_latest=count, archive_path=self.archive)
        for suffix in ("", "-journal", "-wal", "-shm"):
            with self.subTest(suffix=suffix):
                with self.assertRaisesRegex(LedgerError, "SQLite sidecars"):
                    prune(self.db, keep_latest=0, archive_path=str(self.db) + suffix)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(self.archive.exists())
        self.assertEqual(self.prune("--keep-latest", "-1", expected=2).stdout, b"")

    def test_noop_does_not_migrate_or_create_archive_and_ties_are_deterministic(self):
        before = self.db.read_bytes()
        self.assertEqual(prune(self.db, keep_latest=3, archive_path=self.archive)["action"], "unchanged")
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(self.archive.exists())
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE runs SET imported_at='2026-01-01 00:00:00'")
        result = prune(self.db, keep_latest=1, archive_path=self.archive)
        self.assertEqual(result["run_ids"], ["old-ctrf", "old-pw"])  # 'recent' wins the tie.
        self.assertEqual(json.loads(self.cli("report", "--db", self.db).stdout)["schema_version"], 2)
        self.assertIn(b"archived for 2 runs", self.cli("report", "--db", self.db, "--format", "markdown").stdout)
        totals = report(self.db)["totals"]
        # New imports still work after the explicit schema upgrade; archived
        # identical runs remain no-ops even through the bundle importer.
        self.assertEqual(import_ledger(self.db, self.archive.read_bytes())["unchanged_runs"], 2)
        import_report(self.db, PW, format="playwright", run_id="new-run")
        second = self.root / "next.json"
        self.assertEqual(prune(self.db, keep_latest=1, archive_path=second)["run_ids"], ["recent"])
        self.assertEqual(report(self.db)["totals"]["observations"], totals["observations"] + 8)
        third = self.root / "all.json"
        self.assertEqual(prune(self.db, keep_latest=0, archive_path=third)["run_ids"], ["new-run"])
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM attempts").fetchone()[0], 0)
        before = self.db.read_bytes()
        self.assertEqual(prune(self.db, keep_latest=0, archive_path=self.archive)["action"], "unchanged")
        self.assertEqual(self.db.read_bytes(), before)

    def test_corrupt_source_and_foreign_schema_fail_before_archive_publication(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE runs SET digest=? WHERE run_id='old-pw'", ("0" * 64,))
        before = self.db.read_bytes()
        with self.assertRaisesRegex(LedgerError, "SHA-256 mismatch"):
            prune(self.db, keep_latest=1, archive_path=self.archive)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(self.archive.exists())
        foreign = self.root / "foreign.sqlite"
        with closing(sqlite3.connect(foreign)) as db, db:
            db.execute("CREATE TABLE foreign_data (value TEXT)")
        before = foreign.read_bytes()
        for dry_run in (False, True):
            with self.assertRaisesRegex(LedgerError, "not a retry-history-ledger"):
                prune(foreign, keep_latest=0, archive_path=self.archive, dry_run=dry_run)
            self.assertEqual(foreign.read_bytes(), before)
        self.assertFalse(self.archive.exists())

    def test_empty_run_can_be_archived_without_changing_zero_observation_statistics(self):
        value = json.loads(PW)
        value["suites"] = []
        for key in ("expected", "unexpected", "flaky", "skipped"):
            value["stats"][key] = 0
        empty = self.root / "empty.sqlite"
        raw = json.dumps(value).encode()
        import_report(empty, raw, format="playwright", run_id="empty")
        original = report(empty)
        prune(empty, keep_latest=0, archive_path=self.archive)
        self.assertEqual(report(empty)["totals"], original["totals"])
        self.assertEqual(report(empty)["runs"][0]["observations"], [])
        restored = self.root / "restored-empty.sqlite"
        import_ledger(restored, self.archive.read_bytes())
        self.assertEqual(export_ledger(restored), self.archive.read_bytes())
