"""Explicit archive verification contracts, using synthetic offline reports."""

import base64
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from qawrench.flaky import LedgerError, export_ledger, import_report, prune

ROOT = Path(__file__).resolve().parents[2]
PW = (ROOT / "examples/flaky/playwright.json").read_bytes()
CTRF = (ROOT / "examples/flaky/ctrf.json").read_bytes()


class VerifyArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "history.sqlite"
        self.archive = self.root / "old.json"
        import_report(self.db, PW, format="playwright", run_id="old-pw")
        import_report(self.db, CTRF, format="ctrf", project="chromium", run_id="old-ctrf")
        import_report(self.db, PW, format="playwright", run_id="recent")
        self.full_bundle = export_ledger(self.db)
        # Deterministic retention independent of clock resolution.
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE runs SET imported_at=CASE WHEN run_id='recent' THEN '2026-02-01' ELSE '2026-01-01' END")
        prune(self.db, keep_latest=1, archive_path=self.archive)

    def cli(self, path=None, db=None, expected=0):
        result = subprocess.run([sys.executable, "-m", "qawrench.flaky", "verify-archive",
                                 "--db", str(db or self.db), str(path or self.archive)], cwd=self.root,
                                env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, expected, result.stderr.decode())
        self.assertNotIn(b"Traceback", result.stderr)
        return result

    def verify(self, path=None):
        from qawrench.flaky import verify_archive
        return verify_archive(self.db, path or self.archive)

    def reject(self, diagnostic, path=None):
        path = path or self.archive
        before_db = self.db.read_bytes()
        before_archive = path.read_bytes() if path.exists() else None
        result = self.cli(path, expected=2)
        self.assertEqual(result.stdout, b"")
        self.assertIn(diagnostic.encode(), result.stderr)
        self.assertEqual(self.db.read_bytes(), before_db)
        self.assertEqual(path.read_bytes() if path.exists() else None, before_archive)

    def update_json(self, table, column, value, run_id, test_id=None):
        with closing(sqlite3.connect(self.db)) as db, db:
            if test_id is None:
                db.execute(f"UPDATE {table} SET {column}=? WHERE run_id=?", (json.dumps(value), run_id))
            else:
                db.execute(f"UPDATE {table} SET {column}=? WHERE run_id=? AND test_id=?",
                           (json.dumps(value), run_id, test_id))

    def rewrite_bundle(self, bundle):
        self.archive.write_text(json.dumps(bundle))
        digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        with closing(sqlite3.connect(self.db)) as db:
            rows = list(db.execute("SELECT run_id,archive FROM runs WHERE archive IS NOT NULL"))
        for run_id, text in rows:
            reference = json.loads(text)
            reference["sha256"] = digest
            self.update_json("runs", "archive", reference, run_id)

    def test_cli_mixed_archive_contract_is_deterministic_and_read_only(self):
        before = self.db.read_bytes(), self.archive.read_bytes()
        result = self.cli()
        expected = {"action": "verified", "run_ids": ["old-ctrf", "old-pw"],
                    "observations": 14, "attempts": 16,
                    "archive_sha256": hashlib.sha256(before[1]).hexdigest()}
        self.assertEqual(json.loads(result.stdout), expected)
        self.assertEqual(result.stderr, b"")
        self.assertEqual(self.verify(), expected)
        self.assertEqual(self.cli().stdout, result.stdout)
        self.assertEqual((self.db.read_bytes(), self.archive.read_bytes()), before)

    def test_relocated_archive_ignores_stored_paths_and_other_archive_contents(self):
        moved = self.root / "moved.json"
        self.archive.rename(moved)
        # A second archive, which must not be loaded or verified by this call.
        other = self.root / "other.json"
        prune(self.db, keep_latest=0, archive_path=other)
        other.write_bytes(b"corrupt unrelated archive")
        before = self.db.read_bytes()
        self.assertEqual(self.verify(moved)["run_ids"], ["old-ctrf", "old-pw"])
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(self.archive.exists())
        self.assertEqual(other.read_bytes(), b"corrupt unrelated archive")

    def test_bundle_corruption_wrong_bundle_and_missing_files_have_no_stdout(self):
        self.reject("No such file", self.root / "missing.json")
        original = self.archive.read_bytes()
        self.archive.write_bytes(original + b"\n")
        self.reject("archive SHA-256 mismatch")
        self.archive.write_bytes(b"not JSON")
        self.reject("invalid ledger bundle")
        self.archive.write_bytes(self.full_bundle)
        self.reject("archive SHA-256 mismatch")
        self.archive.write_bytes(original)
        missing = self.root / "missing.sqlite"
        failed = self.cli(db=missing, expected=2)
        self.assertEqual(failed.stdout, b"")
        self.assertFalse(missing.exists())
        foreign = self.root / "foreign.sqlite"
        with closing(sqlite3.connect(foreign)) as db:
            db.execute("CREATE TABLE other (x)")
        before = foreign.read_bytes()
        failed = self.cli(db=foreign, expected=2)
        self.assertEqual(failed.stdout, b"")
        self.assertIn(b"not a retry-history-ledger", failed.stderr)
        self.assertEqual(foreign.read_bytes(), before)

    def test_referenced_membership_is_exact_even_with_updated_bundle_digest(self):
        original_bundle = json.loads(self.archive.read_bytes())
        for case in ("missing", "unknown", "full", "empty"):
            with self.subTest(case=case):
                bundle = deepcopy(original_bundle)
                if case == "missing":
                    bundle["runs"].pop()
                    message = "missing referenced run-id"
                elif case == "unknown":
                    bundle["runs"][0]["run_id"] = "unknown"
                    message = "unknown run-id"
                elif case == "full":
                    bundle["runs"].append(next(r for r in json.loads(self.full_bundle)["runs"] if r["run_id"] == "recent"))
                    message = "not archived"
                else:
                    bundle["runs"] = []
                    message = "empty archive"
                self.rewrite_bundle(bundle)
                self.reject(message)

    def test_malformed_embedded_source_is_replayed_and_source_digest_is_validated(self):
        bundle = json.loads(self.archive.read_bytes())
        altered = deepcopy(bundle)
        raw = base64.b64decode(altered["runs"][0]["raw_report_base64"]) + b"\n"
        altered["runs"][0]["raw_report_base64"] = base64.b64encode(raw).decode()
        altered["runs"][0]["sha256"] = hashlib.sha256(raw).hexdigest()
        self.rewrite_bundle(altered)
        self.reject("stored source SHA-256 mismatch")
        bundle["runs"][0]["sha256"] = "0" * 64
        self.rewrite_bundle(bundle)
        self.reject("source SHA-256 mismatch")
        raw = b'{"unsupported":"report"}'
        bundle["runs"][0]["raw_report_base64"] = base64.b64encode(raw).decode()
        bundle["runs"][0]["sha256"] = hashlib.sha256(raw).hexdigest()
        self.rewrite_bundle(bundle)
        self.reject("invalid ledger bundle")

    def test_run_digests_context_metadata_and_archive_reference_tampering(self):
        with closing(sqlite3.connect(self.db)) as db:
            reference = json.loads(db.execute("SELECT archive FROM runs WHERE run_id='old-ctrf'").fetchone()[0])
            metadata = json.loads(db.execute("SELECT metadata FROM runs WHERE run_id='old-ctrf'").fetchone()[0])
        baseline = self.db.read_bytes()
        cases = [("digest", "0" * 64, "source SHA-256 mismatch"),
                 ("format", "playwright", "format/project context mismatch"),
                 ("project_context", "firefox", "format/project context mismatch"),
                 ("archive", json.dumps({**reference, "sha256": "0" * 64}), "archive SHA-256 mismatch"),
                 ("archive", json.dumps({**reference, "source_sha256": "0" * 64}), "source SHA-256 mismatch"),
                 ("archive", "[]", "archive reference"),
                 ("metadata", "{", "invalid retained JSON"),
                 ("metadata", json.dumps({**metadata, "wall_duration_ms": 999}), "metadata.wall_duration_ms"),
                 ("metadata", json.dumps({**metadata, "test_count": 7}), "metadata.test_count")]
        for field, value, message in cases:
            with self.subTest(field=field, value=value):
                self.db.write_bytes(baseline)
                with closing(sqlite3.connect(self.db)) as db, db:
                    db.execute(f"UPDATE runs SET {field}=? WHERE run_id='old-ctrf'", (value,))
                self.reject(message)
        self.db.write_bytes(baseline)
        # Every run metadata field, including errors and loss/completion flags.
        with closing(sqlite3.connect(self.db)) as db:
            rows = list(db.execute("SELECT run_id,metadata FROM runs WHERE archive IS NOT NULL"))
        for run_id, text in rows:
            for field in json.loads(text):
                with self.subTest(run_id=run_id, metadata=field):
                    changed = json.loads(text)
                    changed[field] = None
                    self.update_json("runs", "metadata", changed, run_id)
                    with self.assertRaisesRegex(LedgerError, "metadata"):
                        self.verify()
                    self.update_json("runs", "metadata", json.loads(text), run_id)

    def test_every_retained_observation_field_is_compared_for_both_adapters(self):
        with closing(sqlite3.connect(self.db)) as db:
            rows = list(db.execute("SELECT run_id,test_id,data FROM observations WHERE run_id!='recent'"))
        checked = set()
        for run_id, test_id, text in rows:
            original = json.loads(text)
            # Check each field once per adapter, including each classification.
            paths = [(key,) for key in original if key != "retained_attempts"]
            paths += [("retained_attempts", key) for key in original["retained_attempts"] if key != "classification"]
            paths += [("retained_attempts", "classification", key)
                      for key in original["retained_attempts"]["classification"]]
            for path in paths:
                if (run_id, path) in checked:
                    continue
                checked.add((run_id, path))
                with self.subTest(run_id=run_id, field=path):
                    changed = deepcopy(original)
                    target = changed
                    for key in path[:-1]:
                        target = target[key]
                    target[path[-1]] = "tampered" if target[path[-1]] is None else None
                    self.update_json("observations", "data", changed, run_id, test_id)
                    before = self.db.read_bytes()
                    with self.assertRaisesRegex(LedgerError, "observation"):
                        self.verify()
                    self.assertEqual(self.db.read_bytes(), before)
                    self.update_json("observations", "data", original, run_id, test_id)
        self.assertEqual({run_id for run_id, _ in checked}, {"old-pw", "old-ctrf"})
        self.assertGreaterEqual(len(checked), 50)
        # Type coercion must not hide boolean/count corruption (True == 1).
        run_id, test_id, text = rows[0]
        changed = json.loads(text)
        changed["retained_attempts"]["classification"]["passed"] = 1
        self.update_json("observations", "data", changed, run_id, test_id)
        self.reject("observation")

    def test_observation_identity_sets_and_archived_detail_invariants(self):
        baseline = self.db.read_bytes()
        with closing(sqlite3.connect(self.db)) as db:
            run_id, test_id, text = db.execute("SELECT run_id,test_id,data FROM observations WHERE run_id='old-pw' LIMIT 1").fetchone()
        for statement, parameters, message in (
            ("DELETE FROM observations WHERE run_id=? AND test_id=?", (run_id, test_id), "observation identity set mismatch"),
            ("UPDATE observations SET test_id='extra' WHERE run_id=? AND test_id=?", (run_id, test_id), "observation identity set mismatch"),
            ("UPDATE observations SET data='[]' WHERE run_id=? AND test_id=?", (run_id, test_id), "observation"),
            ("UPDATE runs SET raw_report=X'01' WHERE run_id=?", (run_id,), "archived source blob"),
            ("INSERT INTO attempts VALUES(?,?,0,'passed',1,'{}')", (run_id, test_id), "archived attempt rows"),
        ):
            with self.subTest(statement=statement):
                self.db.write_bytes(baseline)
                with closing(sqlite3.connect(self.db)) as db, db:
                    db.execute(statement, parameters)
                self.reject(message)

    def test_empty_run_is_verified_but_empty_or_unreferenced_bundle_is_rejected(self):
        value = json.loads(PW)
        value["suites"] = []
        for key in ("expected", "unexpected", "flaky", "skipped"):
            value["stats"][key] = 0
        empty_db = self.root / "empty.sqlite"
        import_report(empty_db, json.dumps(value).encode(), format="playwright", run_id="empty")
        empty_archive = self.root / "empty-run.json"
        prune(empty_db, keep_latest=0, archive_path=empty_archive)
        self.db = empty_db
        result = self.cli(empty_archive)
        self.assertEqual(json.loads(result.stdout), {"action": "verified", "run_ids": ["empty"],
            "observations": 0, "attempts": 0, "archive_sha256": hashlib.sha256(empty_archive.read_bytes()).hexdigest()})
        empty = self.root / "no-runs.json"
        empty.write_bytes(export_ledger(empty_db, run_ids=[]))
        self.reject("empty archive", empty)
        full_db = self.root / "full.sqlite"
        import_report(full_db, PW, format="playwright", run_id="old-pw")
        before = full_db.read_bytes()
        failed = self.cli(db=full_db, expected=2)
        self.assertEqual(failed.stdout, b"")
        self.assertEqual(full_db.read_bytes(), before)
