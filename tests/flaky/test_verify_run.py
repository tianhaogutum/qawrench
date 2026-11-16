"""Read-only replay audit contracts for selected local full evidence."""

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

from qawrench.flaky import LedgerError, import_report, prune

ROOT = Path(__file__).resolve().parents[2]
PW = (ROOT / "examples/flaky/playwright.json").read_bytes()
CTRF = (ROOT / "examples/flaky/ctrf.json").read_bytes()


class VerifyRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "history.sqlite"
        import_report(self.db, PW, format="playwright", run_id="pw")
        import_report(self.db, CTRF, format="ctrf", project="chromium", run_id="ctrf")

    def cli(self, run_id="pw", *, expected=0):
        result = subprocess.run([sys.executable, "-m", "qawrench.flaky", "verify-run",
                                 "--db", str(self.db), "--run-id", run_id], cwd=self.root,
                                env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, expected, result.stderr.decode())
        self.assertNotIn(b"Traceback", result.stderr)
        return result

    def verify(self, run_id="pw"):
        from qawrench.flaky import verify_run
        return verify_run(self.db, run_id)

    def reject(self, diagnostic, run_id="pw"):
        before = self.db.read_bytes() if self.db.exists() else None
        result = self.cli(run_id, expected=2)
        self.assertEqual(result.stdout, b"")
        self.assertIn(diagnostic.encode(), result.stderr)
        self.assertEqual(self.db.read_bytes() if self.db.exists() else None, before)

    def execute(self, sql, parameters=()):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute(sql, parameters)

    def test_cli_native_and_ctrf_contract_is_deterministic_and_read_only(self):
        before = self.db.read_bytes()
        for run_id, raw, observations, attempts in (("pw", PW, 8, 9), ("ctrf", CTRF, 6, 7)):
            with self.subTest(run_id=run_id):
                expected = {"action": "verified", "run_id": run_id,
                            "source_sha256": hashlib.sha256(raw).hexdigest(),
                            "observations": observations, "attempts": attempts}
                result = self.cli(run_id)
                self.assertEqual(json.loads(result.stdout), expected)
                self.assertEqual(result.stderr, b"")
                self.assertEqual(self.verify(run_id), expected)
                self.assertEqual(self.cli(run_id).stdout, result.stdout)
        self.assertEqual(self.db.read_bytes(), before)

    def test_source_digest_malformed_source_and_adapter_context_are_checked(self):
        baseline = self.db.read_bytes()
        malformed = b'{"unsupported":"report"}'
        cases = [
            ("UPDATE runs SET digest=? WHERE run_id='pw'", ("0" * 64,), "source SHA-256 mismatch"),
            ("UPDATE runs SET raw_report=? WHERE run_id='pw'", (PW + b"\n",), "source SHA-256 mismatch"),
            ("UPDATE runs SET raw_report=?,digest=? WHERE run_id='pw'",
             (malformed, hashlib.sha256(malformed).hexdigest()), "invalid stored report"),
            ("UPDATE runs SET format='unknown' WHERE run_id='pw'", (), "unsupported format"),
            ("UPDATE runs SET project_context='firefox' WHERE run_id='pw'", (), "--project applies only"),
        ]
        for sql, parameters, message in cases:
            with self.subTest(sql=sql):
                self.db.write_bytes(baseline)
                self.execute(sql, parameters)
                self.reject(message)
        self.db.write_bytes(baseline)
        self.execute("UPDATE runs SET project_context='firefox' WHERE run_id='ctrf'")
        self.reject("observation identity set mismatch", "ctrf")

    def test_all_metadata_observation_and_attempt_fields_match_replay(self):
        with closing(sqlite3.connect(self.db)) as db:
            # One complete row per adapter, plus every available attempt row.
            metadata = list(db.execute("SELECT run_id,metadata FROM runs"))
            observations = list(db.execute("SELECT run_id,test_id,data FROM observations"))
            attempts = list(db.execute("SELECT run_id,test_id,attempt_index,data FROM attempts"))
        checked = set()
        for table, rows in (("runs", metadata), ("observations", observations), ("attempts", attempts)):
            column = "metadata" if table == "runs" else "data"
            for *identity, text in rows:
                original = json.loads(text)
                for field in original:
                    key = table, identity[0], field
                    if key in checked:
                        continue
                    checked.add(key)
                    changed = deepcopy(original)
                    changed[field] = "tampered" if original[field] is None else None
                    where = " AND ".join(f"{name}=?" for name in ("run_id", "test_id", "attempt_index")[:len(identity)])
                    with self.subTest(table=table, run_id=identity[0], field=field):
                        self.execute(f"UPDATE {table} SET {column}=? WHERE {where}", (json.dumps(changed), *identity))
                        before = self.db.read_bytes()
                        with self.assertRaisesRegex(LedgerError, "metadata|observation|attempt"):
                            self.verify(identity[0])
                        self.assertEqual(self.db.read_bytes(), before)
                        self.execute(f"UPDATE {table} SET {column}=? WHERE {where}", (text, *identity))
        self.assertEqual({key[:2] for key in checked},
                         {(table, run_id) for table in ("runs", "observations", "attempts") for run_id in ("pw", "ctrf")})
        # A JSON boolean/count substitution must fail even though True == 1.
        run_id, test_id, text = observations[0]
        changed = json.loads(text)
        changed["reported_flaky"] = int(changed["reported_flaky"])
        self.execute("UPDATE observations SET data=? WHERE run_id=? AND test_id=?",
                     (json.dumps(changed), run_id, test_id))
        self.reject("observation", run_id)

    def test_relational_attempt_columns_membership_and_malformed_json_are_checked(self):
        with closing(sqlite3.connect(self.db)) as db:
            test_id, index, text = db.execute(
                "SELECT test_id,attempt_index,data FROM attempts WHERE run_id='pw' LIMIT 1").fetchone()
        baseline = self.db.read_bytes()
        where = " WHERE run_id='pw' AND test_id=? AND attempt_index=?"
        cases = [
            ("UPDATE attempts SET status='tampered'" + where, (test_id, index), "status"),
            ("UPDATE attempts SET duration_ms=999" + where, (test_id, index), "duration_ms"),
            ("UPDATE attempts SET duration_ms='corrupt'" + where, (test_id, index), "duration_ms"),
            ("UPDATE attempts SET attempt_index=999" + where, (test_id, index), "attempt identity set mismatch"),
            ("UPDATE attempts SET test_id='orphan'" + where, (test_id, index), "attempt identity set mismatch"),
            ("DELETE FROM attempts" + where, (test_id, index), "attempt identity set mismatch"),
            ("INSERT INTO attempts VALUES('pw',?,999,'passed',1,?)", (test_id, text), "attempt identity set mismatch"),
            ("DELETE FROM observations WHERE run_id='pw' AND test_id=?", (test_id,), "observation identity set mismatch"),
            ("UPDATE observations SET test_id='extra' WHERE run_id='pw' AND test_id=?", (test_id,), "observation identity set mismatch"),
            ("UPDATE runs SET metadata='[]' WHERE run_id='pw'", (), "metadata"),
            ("UPDATE observations SET data='{' WHERE run_id='pw' AND test_id=?", (test_id,), "invalid retained JSON"),
            ("UPDATE attempts SET data='[]'" + where, (test_id, index), "attempt"),
            ("UPDATE attempts SET data='{\"index\":0,\"index\":1}'" + where, (test_id, index), "duplicate object key"),
        ]
        for sql, parameters, message in cases:
            with self.subTest(sql=sql):
                self.db.write_bytes(baseline)
                self.execute(sql, parameters)
                self.reject(message)

    def test_exact_selection_missing_foreign_and_archived_runs(self):
        for run_id in ("missing", "PW", " pw ", " "):
            self.reject("nonempty string" if run_id == " " else "unknown run-id", run_id)
        for run_id in (None, [], 1, "\ud800"):
            with self.subTest(run_id=repr(run_id)), self.assertRaises(LedgerError):
                self.verify(run_id)
        # v2 full-run verification works without opening an archived path.
        self.execute("UPDATE runs SET imported_at=CASE WHEN run_id='pw' THEN '2026-02-01' ELSE '2026-01-01' END")
        archive = self.root / "archive.json"
        prune(self.db, keep_latest=1, archive_path=archive)
        archive.write_bytes(b"stored archive path must not be read")
        self.assertEqual(self.verify()["attempts"], 9)
        self.reject("use verify-archive", "ctrf")
        self.assertEqual(archive.read_bytes(), b"stored archive path must not be read")
        # Only selected evidence is audited; corrupt unrelated metrics do not block it.
        self.execute("UPDATE runs SET metadata='corrupt' WHERE run_id='ctrf'")
        self.assertEqual(self.verify()["observations"], 8)
        self.db = self.root / "missing.sqlite"
        self.reject("cannot read ledger")
        self.db = self.root / "foreign.sqlite"
        self.execute("CREATE TABLE other (x)")
        self.reject("not a retry-history-ledger")

    def test_zero_observations_and_fractional_durations(self):
        for format, raw, project in (("playwright", PW, None), ("ctrf", CTRF, "chromium")):
            value = json.loads(raw)
            if format == "playwright":
                value["suites"] = []
                for key in ("expected", "unexpected", "flaky", "skipped"):
                    value["stats"][key] = 0
            else:
                value["results"]["tests"] = []
                for key in ("tests", "passed", "failed", "skipped", "pending", "other"):
                    value["results"]["summary"][key] = 0
            run_id = "empty-" + format
            import_report(self.db, json.dumps(value).encode(), format=format, run_id=run_id, project=project)
            result = json.loads(self.cli(run_id).stdout)
            self.assertEqual((result["observations"], result["attempts"]), (0, 0))
        value = json.loads(CTRF)
        value["results"]["tests"][0]["duration"] = 0.25
        import_report(self.db, json.dumps(value).encode(), format="ctrf", project="chromium", run_id="fractional")
        self.assertEqual(self.verify("fractional")["attempts"], 7)
        self.execute("INSERT INTO attempts VALUES('empty-playwright','orphan',0,'passed',1,'{}')")
        self.reject("attempt identity set mismatch", "empty-playwright")
