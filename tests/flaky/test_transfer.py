"""Synthetic two-worker histories; no network, browser or production data."""

import base64
import copy
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

from qawrench.flaky import (LedgerError, export_ledger, import_ledger,
                                  import_report, raw_report, report)

ROOT = Path(__file__).resolve().parents[2]
PW = (ROOT / "examples/flaky/playwright.json").read_bytes()
CTRF = (ROOT / "examples/flaky/ctrf.json").read_bytes()


def encoded(value):
    return json.dumps(value, allow_nan=False).encode()


def record(run_id, source=PW, format="playwright", project=None):
    return {"run_id": run_id, "format": format, "project_context": project,
            "sha256": hashlib.sha256(source).hexdigest(),
            "raw_report_base64": base64.b64encode(source).decode("ascii")}


def bundle(*runs):
    return {"ledger_format": "retry-history-ledger", "version": 1, "runs": list(runs)}


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "target.sqlite"

    def test_deterministic_export_independent_of_insertion_order_and_local_time(self):
        other = self.root / "other.sqlite"
        entries = [("試験", PW, "playwright", None), ("a", CTRF, "ctrf", "chromium")]
        for path, order in ((self.db, entries), (other, reversed(entries))):
            for run_id, raw, format, project in order:
                import_report(path, raw, run_id=run_id, format=format, project=project)
        with closing(sqlite3.connect(other)) as db, db:
            db.execute("UPDATE runs SET imported_at='2000-01-01 00:00:00'")
        before = self.db.read_bytes()
        result = export_ledger(self.db)
        self.assertEqual(result, export_ledger(other))
        self.assertEqual(result, export_ledger(self.db))
        self.assertEqual(self.db.read_bytes(), before)
        expected = bundle(record("a", CTRF, "ctrf", "chromium"), record("試験"))
        canonical = json.dumps(expected, ensure_ascii=True, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("ascii") + b"\n"
        self.assertEqual(result, canonical)
        self.assertNotIn(b"imported_at", result)

    def test_roundtrip_rebuilds_same_evidence_and_exact_sources(self):
        raw = b"\xef\xbb\xbf" + PW + b"\n "
        import_report(self.db, raw, format="playwright", run_id="native")
        import_report(self.db, CTRF, format="ctrf", project="firefox", run_id="ctrf")
        original_bundle = export_ledger(self.db)
        restored = self.root / "restored.sqlite"
        self.assertEqual(import_ledger(restored, original_bundle),
                         {"action": "merged", "imported_runs": 2, "unchanged_runs": 0})
        self.assertEqual(report(restored), report(self.db))
        self.assertEqual(raw_report(restored, "native"), raw)
        self.assertEqual(raw_report(restored, "ctrf"), CTRF)
        self.assertEqual(export_ledger(restored), original_bundle)

    def test_worker_merge_order_does_not_change_export_or_recurrence(self):
        a = encoded(bundle(record("shared"), record("worker-a")))
        b = encoded(bundle(record("shared"), record("worker-b", CTRF, "ctrf", "chromium")))
        other = self.root / "other.sqlite"
        for path, first, second in ((self.db, a, b), (other, b, a)):
            import_ledger(path, first)
            self.assertEqual(import_ledger(path, second),
                             {"action": "merged", "imported_runs": 1, "unchanged_runs": 1})
        self.assertEqual(export_ledger(self.db), export_ledger(other))
        data = report(self.db)
        recurring = [t for t in data["tests"] if t["recurring_fail_then_pass"]]
        self.assertEqual(len(recurring), 1)
        self.assertEqual(recurring[0]["fail_then_pass_runs"], ["shared", "worker-a"])

    def test_identical_duplicates_in_bundle_count_once_and_reimport_is_noop(self):
        raw = encoded(bundle(record("shared"), record("shared")))
        self.assertEqual(import_ledger(self.db, raw),
                         {"action": "merged", "imported_runs": 1, "unchanged_runs": 0})
        before = self.db.read_bytes()
        self.assertEqual(import_ledger(self.db, raw),
                         {"action": "unchanged", "imported_runs": 0, "unchanged_runs": 1})
        self.assertEqual(self.db.read_bytes(), before)

    def test_conflicting_duplicate_in_bundle_rejected_before_opening_sqlite(self):
        for conflicting in (record("shared", PW + b"\n"),
                            record("shared", CTRF, "ctrf", "chromium")):
            with self.subTest(conflicting=conflicting["format"]):
                raw = encoded(bundle(record("a-new"), record("shared"), conflicting))
                with self.assertRaisesRegex(LedgerError, "conflicting duplicate run-id"):
                    import_ledger(self.db, raw)
                self.assertFalse(self.db.exists())

    def test_conflicts_in_bytes_format_or_project_rollback_earlier_run(self):
        import_report(self.db, CTRF, format="ctrf", project="chromium", run_id="z-shared")
        before = self.db.read_bytes()
        for conflicting in (record("z-shared", CTRF + b"\n", "ctrf", "chromium"),
                            record("z-shared", PW),
                            record("z-shared", CTRF, "ctrf", "firefox")):
            with self.subTest(format=conflicting["format"], project=conflicting["project_context"]):
                with self.assertRaisesRegex(LedgerError, "already exists"):
                    import_ledger(self.db, encoded(bundle(record("a-new"), conflicting)))
                self.assertEqual(self.db.read_bytes(), before)
                self.assertEqual(report(self.db)["run_count"], 1)

    def test_storage_failure_on_second_run_rolls_back_whole_bundle(self):
        import_report(self.db, PW, format="playwright", run_id="original")
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON attempts WHEN NEW.run_id='b' "
                       "BEGIN SELECT RAISE(ABORT, 'synthetic second-run failure'); END")
        before = self.db.read_bytes()
        with self.assertRaisesRegex(LedgerError, "rolled back.*synthetic second-run failure"):
            import_ledger(self.db, encoded(bundle(record("a"), record("b"))))
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(report(self.db)["run_count"], 1)

    def test_malformed_bundle_and_embedded_reports_do_not_touch_database(self):
        good = bundle(record("a"), record("b"))
        mutations = [
            lambda v: v.update(ledger_format="other"),
            lambda v: v.update(version=2),
            lambda v: v.update(version=True),
            lambda v: v.update(version=1.0),
            lambda v: v.update(extra="unknown"),
            lambda v: v.pop("version"),
            lambda v: v.update(runs={}),
            lambda v: v["runs"].__setitem__(1, None),
            lambda v: v["runs"][1].pop("sha256"),
            lambda v: v["runs"][1].update(extra=True),
            lambda v: v["runs"][1].update(run_id=" "),
            lambda v: v["runs"][1].update(run_id="\ud800"),
            lambda v: v["runs"][1].update(format="future"),
            lambda v: v["runs"][1].update(project_context="native-does-not-allow-context"),
            lambda v: v["runs"][1].update(project_context="\ud800"),
            lambda v: v["runs"][1].update(raw_report_base64="!not-base64!"),
            lambda v: v["runs"][1].update(raw_report_base64="試験"),
            lambda v: v["runs"][1].update(sha256="0" * 64),
            lambda v: v["runs"].__setitem__(1, record("b", b"not a report")),
            lambda v: v["runs"].__setitem__(1, record("b", CTRF, "ctrf", None)),
        ]
        invalid = [b"{", b"[]", b"\xff", b'{"version":1,"version":1}',
                   b'{"x":NaN}', b'{"x":1e999}', b"[" * 1500 + b"]" * 1500]
        for mutate in mutations:
            value = copy.deepcopy(good)
            mutate(value)
            invalid.append(encoded(value))
        existing = self.root / "existing.sqlite"
        import_report(existing, PW, format="playwright", run_id="original")
        before = existing.read_bytes()
        for raw in invalid:
            with self.subTest(raw=raw[:100]):
                for target in (self.db, existing):
                    with self.assertRaisesRegex(LedgerError, "invalid ledger bundle"):
                        import_ledger(target, raw)
                self.assertFalse(self.db.exists())
                self.assertEqual(existing.read_bytes(), before)

    def test_missing_foreign_and_unsupported_databases_are_not_overwritten(self):
        with self.assertRaises(LedgerError):
            export_ledger(self.db)
        self.assertFalse(self.db.exists())
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("CREATE TABLE foreign_data (value TEXT)")
        before = self.db.read_bytes()
        for operation in (lambda: export_ledger(self.db),
                          lambda: import_ledger(self.db, encoded(bundle(record("a"))))):
            with self.assertRaisesRegex(LedgerError, "not a retry-history-ledger"):
                operation()
            self.assertEqual(self.db.read_bytes(), before)
        self.db.unlink()
        import_ledger(self.db, encoded(bundle(record("a"))))
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("PRAGMA user_version=999")
        before = self.db.read_bytes()
        with self.assertRaisesRegex(LedgerError, "no automatic migration"):
            import_ledger(self.db, encoded(bundle(record("b"))))
        self.assertEqual(self.db.read_bytes(), before)

    def test_empty_ledger_roundtrip(self):
        self.assertEqual(import_ledger(self.db, encoded(bundle())),
                         {"action": "unchanged", "imported_runs": 0, "unchanged_runs": 0})
        self.assertEqual(json.loads(export_ledger(self.db)), bundle())
        self.assertEqual(report(self.db)["run_count"], 0)

    def test_export_rejects_corrupted_source_digest(self):
        import_report(self.db, PW, format="playwright", run_id="a")
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE runs SET digest=?", ("0" * 64,))
        with self.assertRaisesRegex(LedgerError, "stored source SHA-256 mismatch"):
            export_ledger(self.db)

    def test_selection_is_canonical_deduplicated_and_roundtrips_exact_sources(self):
        raw = b"\xef\xbb\xbf" + PW + b"\n "
        import_report(self.db, raw, format="playwright", run_id="試験")
        import_report(self.db, CTRF, format="ctrf", project="firefox", run_id="a' OR 1=1 --")
        import_report(self.db, PW, format="playwright", run_id="unselected")
        before = self.db.read_bytes()
        ids = ["試験", "a' OR 1=1 --", "試験"]
        result = export_ledger(self.db, run_ids=ids)
        expected = bundle(record("a' OR 1=1 --", CTRF, "ctrf", "firefox"), record("試験", raw))
        canonical = json.dumps(expected, ensure_ascii=True, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("ascii") + b"\n"
        self.assertEqual(result, canonical)
        self.assertEqual(result, export_ledger(self.db, run_ids=reversed(ids)))
        self.assertEqual(ids, ["試験", "a' OR 1=1 --", "試験"])
        self.assertEqual(self.db.read_bytes(), before)
        restored = self.root / "selected.sqlite"
        self.assertEqual(import_ledger(restored, result),
                         {"action": "merged", "imported_runs": 2, "unchanged_runs": 0})
        self.assertEqual(raw_report(restored, "試験"), raw)
        self.assertEqual(raw_report(restored, "a' OR 1=1 --"), CTRF)
        self.assertEqual(export_ledger(restored), result)
        with self.assertRaisesRegex(LedgerError, "unknown run-id"):
            raw_report(restored, "unselected")

    def test_selection_none_all_and_empty_have_explicit_meanings(self):
        import_report(self.db, PW, format="playwright", run_id="a")
        import_report(self.db, CTRF, format="ctrf", project="chromium", run_id="b")
        self.assertEqual(export_ledger(self.db, run_ids=None), export_ledger(self.db))
        self.assertEqual(export_ledger(self.db, run_ids=["b", "a", "b"]), export_ledger(self.db))
        empty = export_ledger(self.db, run_ids=[])
        self.assertEqual(empty, b'{"ledger_format":"retry-history-ledger","runs":[],"version":1}\n')
        restored = self.root / "empty.sqlite"
        import_ledger(restored, empty)
        self.assertEqual(export_ledger(restored), empty)

    def test_selection_unknown_and_invalid_ids_do_not_modify_database(self):
        import_report(self.db, PW, format="playwright", run_id="known")
        before = self.db.read_bytes()
        with self.assertRaisesRegex(LedgerError, "unknown run-id.*'a-missing'.*'z-missing'"):
            export_ledger(self.db, run_ids=["known", "z-missing", "a-missing", "z-missing"])
        # A scalar string must not accidentally select its individual characters.
        for invalid in ("known", b"known", 1, [None], [1], [""], [" \t"], ["\ud800"]):
            with self.subTest(invalid=repr(invalid)):
                with self.assertRaisesRegex(LedgerError, "run_ids"):
                    export_ledger(self.db, run_ids=invalid)
                self.assertEqual(self.db.read_bytes(), before)
        missing = self.root / "missing.sqlite"
        with self.assertRaises(LedgerError):
            export_ledger(missing, run_ids=["known"])
        self.assertFalse(missing.exists())

    def test_selection_only_checks_requested_source_digests(self):
        import_report(self.db, PW, format="playwright", run_id="good")
        import_report(self.db, PW, format="playwright", run_id="bad")
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE runs SET digest=? WHERE run_id='bad'", ("0" * 64,))
        before = self.db.read_bytes()
        self.assertEqual(json.loads(export_ledger(self.db, run_ids=["good"])), bundle(record("good")))
        with self.assertRaisesRegex(LedgerError, "stored source SHA-256 mismatch"):
            export_ledger(self.db, run_ids=["good", "bad"])
        self.assertEqual(self.db.read_bytes(), before)


class TransferCLITests(unittest.TestCase):
    def test_selection_cli_contract_and_no_stdout_on_rejection(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)

            def cli(*args, expected=0):
                result = subprocess.run(
                    [sys.executable, "-m", "qawrench.flaky", *map(str, args)],
                    cwd=temp, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                    capture_output=True, timeout=10)
                self.assertEqual(result.returncode, expected, result.stderr.decode())
                self.assertNotIn(b"Traceback", result.stderr)
                return result

            source = temp / "bom.json"
            raw = b"\xef\xbb\xbf" + PW + b"\n "
            source.write_bytes(raw)
            db = temp / "worker.sqlite"
            cli("import", "--db", db, "--format", "playwright", "--run-id", "試験", source)
            cli("import", "--db", db, "--format", "playwright", "--run-id", "excluded", source)
            cli("import", "--db", db, "--format", "ctrf", "--project", "chromium",
                "--run-id", "worker-b", ROOT / "examples/flaky/ctrf.json")
            before = db.read_bytes()
            args = ("export-ledger", "--db", db)
            selected = cli(*args, "--run-id", "試験", "--run-id", "worker-b", "--run-id", "試験")
            self.assertEqual(selected.stderr, b"")
            self.assertEqual(selected.stdout, cli(*args, "--run-id", "worker-b", "--run-id", "試験").stdout)
            self.assertEqual(json.loads(selected.stdout),
                             bundle(record("worker-b", CTRF, "ctrf", "chromium"), record("試験", raw)))
            path = temp / "selected.json"
            path.write_bytes(selected.stdout)
            restored = temp / "restored.sqlite"
            outcome = json.loads(cli("import-ledger", "--db", restored, path).stdout)
            self.assertEqual(outcome, {"action": "merged", "imported_runs": 2, "unchanged_runs": 0})
            self.assertEqual(cli("export-ledger", "--db", restored).stdout, selected.stdout)
            self.assertEqual(cli("export", "--db", restored, "--run-id", "試験").stdout, raw)
            self.assertEqual(cli("export", "--db", restored, "--run-id", "worker-b").stdout, CTRF)
            for invalid, diagnostic in (("unknown", b"unknown run-id"), ("Worker-b", b"unknown run-id"),
                                        ("", b"run_ids"), (" \t", b"run_ids")):
                with self.subTest(invalid=invalid):
                    failed = cli(*args, "--run-id", "worker-b", "--run-id", invalid, expected=2)
                    self.assertEqual(failed.stdout, b"")
                    self.assertIn(diagnostic, failed.stderr)
            self.assertEqual(db.read_bytes(), before)

    def test_two_worker_merge_is_idempotent_and_conflict_is_atomic(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)

            def cli(*args, expected=0):
                result = subprocess.run(
                    [sys.executable, "-m", "qawrench.flaky", *map(str, args)],
                    cwd=temp, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                    capture_output=True, timeout=10)
                self.assertEqual(result.returncode, expected, result.stderr.decode())
                self.assertNotIn(b"Traceback", result.stderr)
                return result

            def native(db, run_id, source=ROOT / "examples/flaky/playwright.json"):
                cli("import", "--db", db, "--run-id", run_id,
                    "--format", "playwright", source)

            a, b, merged = (temp / name for name in ("a.sqlite", "b.sqlite", "merged.sqlite"))
            native(a, "shared")
            native(a, "worker-a")
            native(b, "shared")
            cli("import", "--db", b, "--run-id", "worker-b", "--format", "ctrf",
                "--project", "chromium", ROOT / "examples/flaky/ctrf.json")
            for worker, name in ((a, "a.json"), (b, "b.json")):
                bundle = temp / name
                bundle.write_bytes(cli("export-ledger", "--db", worker).stdout)
                outcome = json.loads(cli("import-ledger", "--db", merged, bundle).stdout)
                self.assertEqual(outcome["imported_runs"], 2 if worker == a else 1)
                self.assertEqual(outcome["unchanged_runs"], 0 if worker == a else 1)
            summary = json.loads(cli("report", "--db", merged).stdout)
            self.assertEqual(summary["run_count"], 3)
            self.assertEqual(summary["totals"]["observations"], 22)
            self.assertEqual(summary["totals"]["available_attempts"], 25)
            before = merged.read_bytes()
            outcome = json.loads(cli("import-ledger", "--db", merged, temp / "b.json").stdout)
            self.assertEqual(outcome, {"action": "unchanged", "imported_runs": 0,
                                       "unchanged_runs": 2})
            self.assertEqual(merged.read_bytes(), before)
            self.assertEqual(cli("export", "--db", merged, "--run-id", "worker-b").stdout,
                             (ROOT / "examples/flaky/ctrf.json").read_bytes())

            conflict_db = temp / "conflict.sqlite"
            native(conflict_db, "a-new-run")  # Must be rolled back when 'shared' conflicts.
            changed = temp / "changed.json"
            changed.write_bytes((ROOT / "examples/flaky/playwright.json").read_bytes() + b"\n")
            native(conflict_db, "shared", changed)
            conflict = temp / "conflict.json"
            conflict.write_bytes(cli("export-ledger", "--db", conflict_db).stdout)
            rejected = cli("import-ledger", "--db", merged, conflict, expected=2)
            self.assertIn(b"already exists", rejected.stderr)
            self.assertEqual(merged.read_bytes(), before)
