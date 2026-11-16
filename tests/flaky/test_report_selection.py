"""Exact report scopes for synthetic full and archived native/CTRF histories."""

from contextlib import closing
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from qawrench.flaky import LedgerError, import_ledger, import_report, prune, report
from qawrench.flaky.ledger import markdown

ROOT = Path(__file__).resolve().parents[2]
PW = (ROOT / 'examples/flaky/playwright.json').read_bytes()
CTRF = (ROOT / 'examples/flaky/ctrf.json').read_bytes()


class ReportSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'history.sqlite'
        empty = json.loads(PW)
        empty['suites'] = []
        for key in ('expected', 'unexpected', 'flaky', 'skipped'):
            empty['stats'][key] = 0
        self.empty = json.dumps(empty).encode()
        for run_id, raw, format, project in (
                ('01-pw', PW, 'playwright', None), ('02-ctrf', CTRF, 'ctrf', 'chromium'),
                ('03-empty', self.empty, 'playwright', None), ('04-pw', PW, 'playwright', None),
                ('05-ctrf', CTRF, 'ctrf', 'chromium'), ('06-empty', self.empty, 'playwright', None)):
            import_report(self.db, raw, format=format, project=project, run_id=run_id)
        self.execute("UPDATE runs SET imported_at='2026-01-01'")
        self.archive = self.root / 'old.json'
        prune(self.db, keep_latest=3, archive_path=self.archive)

    def execute(self, sql, params=()):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute(sql, params)

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, '-m', 'qawrench.flaky', 'report',
                                 '--db', str(self.db), *args], cwd=self.root,
                                env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')}, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, expected, result.stderr.decode())
        self.assertNotIn(b'Traceback', result.stderr)
        if expected == 0:
            self.assertEqual(result.stderr, b'')
        return result

    def test_cli_mixed_selection_contract_and_markdown(self):
        before = self.db.read_bytes(), self.archive.read_bytes()
        args = ('--run-id', '06-empty', '--run-id', '04-pw', '--run-id', '02-ctrf',
                '--run-id', '01-pw', '--run-id', '04-pw')
        result = self.cli(*args)
        data = json.loads(result.stdout)
        self.assertEqual([r['run_id'] for r in data['runs']], ['01-pw', '02-ctrf', '04-pw', '06-empty'])
        self.assertEqual((data['run_count'], data['unique_tests']), (4, 14))
        self.assertEqual((data['totals']['observations'], data['totals']['available_attempts']), (22, 25))
        self.assertEqual((data['totals']['fail_then_pass'], data['totals']['mapped_fail_then_pass']), (2, 1))
        self.assertEqual(data['totals']['attempt_details_unavailable'], 3)
        self.assertEqual(sum(t['recurring_fail_then_pass'] for t in data['tests']), 1)
        for history in data['tests']:
            self.assertTrue(set(history['run_ids']) <= {'01-pw', '02-ctrf', '04-pw'})
        self.assertEqual(data['runs'][-1]['observations'], [])
        self.assertEqual(sum('archive' in r for r in data['runs']), 2)
        self.assertEqual(self.cli(*args, '--format', 'markdown').stdout.decode(), markdown(data) + '\n')
        self.assertIn('Runs: 4; unique tests: 14', markdown(data))
        self.assertIn('Full attempts/source archived for 2 runs', markdown(data))
        self.assertEqual(self.cli('--run-id', '01-pw', '--run-id', '02-ctrf', '--run-id', '04-pw',
                                  '--run-id', '06-empty').stdout, result.stdout)
        self.assertEqual((self.db.read_bytes(), self.archive.read_bytes()), before)

    def test_library_scopes_totals_recurrence_and_available_evidence(self):
        all_runs = report(self.db)
        self.assertEqual(report(self.db, run_ids=None), all_runs)
        self.assertEqual(report(self.db, run_ids=[r['run_id'] for r in all_runs['runs']]), all_runs)
        one = report(self.db, run_ids=['04-pw', '04-pw'])
        self.assertEqual(one['runs'], [all_runs['runs'][3]])
        self.assertEqual(one['totals'], one['runs'][0]['totals'])
        self.assertFalse(any(t['recurring_fail_then_pass'] for t in one['tests']))
        ctrf = report(self.db, run_ids=(r for r in ['05-ctrf', '02-ctrf', '05-ctrf']))
        self.assertEqual((ctrf['run_count'], ctrf['unique_tests']), (2, 6))
        self.assertEqual((ctrf['totals']['observations'], ctrf['totals']['available_attempts']), (12, 14))
        self.assertEqual(ctrf['totals']['mapped_fail_then_pass'], 2)
        self.assertEqual(sum(t['recurring_reported_flaky'] for t in ctrf['tests']), 2)
        archived, full = (r['observations'] for r in ctrf['runs'])
        self.assertTrue(all(o['attempts_archived'] and not o['attempts'] for o in archived))
        aggregate = next(o for o in archived if o['title_path'][-1] == 'aggregate retries')
        self.assertEqual(aggregate['available_attempt_count'], 1)
        self.assertTrue(aggregate['attempt_details_unavailable'])
        self.assertEqual(sum(len(o['attempts']) for o in full), 7)
        zeros = report(self.db, run_ids=['06-empty', '03-empty'])
        self.assertEqual((zeros['run_count'], zeros['unique_tests']), (2, 0))
        self.assertEqual(zeros['totals']['observations'], 0)
        empty = report(self.db, run_ids=[])
        self.assertEqual((empty['run_count'], empty['unique_tests'], empty['runs'], empty['tests']), (0, 0, [], []))
        self.assertEqual(empty['totals']['available_attempts'], 0)
        self.assertEqual(empty['schema_version'], 2)

    def test_v1_empty_and_exact_literal_ids(self):
        self.db = self.root / 'v1.sqlite'
        import_ledger(self.db, b'{"ledger_format":"retry-history-ledger","version":1,"runs":[]}')
        self.assertEqual(report(self.db, run_ids=[]), report(self.db))
        ids = ['same', 'Same', ' same ', '試験', "quote' ?", '--literal']
        for run_id in ids:
            import_report(self.db, self.empty, format='playwright', run_id=run_id)
        before = self.db.read_bytes()
        flags = [f'--run-id={run_id}' for run_id in reversed(ids)]
        data = json.loads(self.cli(*flags).stdout)
        self.assertEqual([r['run_id'] for r in data['runs']], sorted(ids))
        self.assertEqual(data['schema_version'], 1)
        self.assertEqual([r['run_id'] for r in report(self.db, run_ids=['Same'])['runs']], ['Same'])
        self.assertEqual(self.db.read_bytes(), before)

    def test_invalid_selections_fail_before_opening_database(self):
        for ids in ('04-pw', b'04-pw', 7, [None], [''], [' '], ['\ud800'], [[]], [True]):
            with self.subTest(ids=repr(ids)), patch('qawrench.flaky.ledger._read_db') as read:
                with self.assertRaisesRegex(LedgerError, 'invalid run_ids'):
                    report(self.db, run_ids=ids)
                read.assert_not_called()

    def test_cli_unknown_invalid_and_selected_corruption_fail_closed(self):
        for format in ('json', 'markdown'):
            for run_id, diagnostic in (('04-PW', b'unknown run-id'), ('', b'invalid run_ids'),
                                        (' ', b'invalid run_ids')):
                before = self.db.read_bytes()
                result = self.cli('--format', format, '--run-id', '04-pw', '--run-id', run_id, expected=2)
                self.assertEqual(result.stdout, b'')
                self.assertIn(diagnostic, result.stderr)
                self.assertEqual(self.db.read_bytes(), before)
        self.execute("UPDATE runs SET metadata='{' WHERE run_id='04-pw'")
        before = self.db.read_bytes()
        # Membership must be checked before decoding selected evidence.
        result = self.cli('--run-id', '04-pw', '--run-id', 'missing', expected=2)
        self.assertIn(b'unknown run-id', result.stderr)
        result = self.cli('--run-id', '04-pw', expected=2)
        self.assertEqual(result.stdout, b'')
        self.assertIn(b'cannot summarize ledger', result.stderr)
        self.assertEqual(self.db.read_bytes(), before)
        self.db = self.root / 'missing.sqlite'
        result = self.cli('--run-id', 'missing', expected=2)
        self.assertEqual(result.stdout, b'')
        self.assertIn(b'cannot read ledger', result.stderr)
        self.assertFalse(self.db.exists())

    def test_unselected_corruption_no_replay_or_external_path_reads(self):
        selected = ['01-pw', '04-pw']
        expected = report(self.db, run_ids=selected)
        self.archive.unlink()
        self.execute("UPDATE runs SET metadata=CAST(x'80' AS TEXT),archive='{' WHERE run_id='02-ctrf'")
        self.execute("UPDATE observations SET data='{' WHERE run_id='05-ctrf'")
        self.execute("UPDATE attempts SET data='{' WHERE run_id='05-ctrf'")
        self.execute('UPDATE runs SET raw_report=?', (b'corrupt source',))
        before = self.db.read_bytes()
        with patch('pathlib.Path.open', side_effect=AssertionError('external file read')), \
                patch('builtins.open', side_effect=AssertionError('external file read')), \
                patch('qawrench.flaky.ledger.parse_report', side_effect=AssertionError('replay')):
            self.assertEqual(report(self.db, run_ids=selected), expected)
            self.assertEqual(report(self.db, run_ids=[])['runs'], [])
        self.assertEqual(json.loads(self.cli('--run-id', '04-pw', '--run-id', '01-pw').stdout), expected)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(self.archive.exists())

    def test_valid_archived_statuses_zero_attempts_and_large_duration_sums(self):
        # Preserve every supported adapter status, including CTRF-only statuses,
        # and sums above the per-attempt input limit.
        value = json.loads(CTRF)
        template = value['results']['tests'][0]
        value['results']['tests'] = [
            {**{k: v for k, v in template.items() if k not in ('rawStatus', 'retryAttempts')},
             'name': status, 'status': status, 'retries': 0, 'flaky': False}
            for status in ('pending', 'other')]
        value['results']['summary'].update(tests=2, passed=0, failed=0, skipped=0, pending=1, other=1)
        self.db = self.root / 'boundaries.sqlite'
        import_report(self.db, json.dumps(value).encode(), format='ctrf', project='chromium', run_id='ctrf')
        value = json.loads(PW)
        def enlarge(suites):
            for suite in suites:
                for spec in suite['specs']:
                    for test in spec['tests']:
                        for result in test['results']:
                            result['duration'] = 2**53 - 1
                enlarge(suite.get('suites', []))
        enlarge(value['suites'])
        import_report(self.db, json.dumps(value).encode(), format='playwright', run_id='native')
        original = report(self.db)
        archive = self.root / 'boundaries.json'
        prune(self.db, keep_latest=0, archive_path=archive)
        archive.unlink()
        before = self.db.read_bytes()
        archived = report(self.db)
        self.assertEqual(archived['totals'], original['totals'])
        self.assertEqual(archived['tests'], original['tests'])
        self.assertEqual({o['terminal_status'] for o in archived['runs'][0]['observations']}, {'pending', 'other'})
        native = archived['runs'][1]['observations']
        self.assertTrue(any(o['available_attempt_count'] == 0 and o['not_run'] for o in native))
        self.assertTrue(any(o['available_attempt_duration_ms'] > 2**53 - 1 for o in native))
        args = ('--run-id', 'native', '--run-id', 'ctrf')
        self.assertEqual(json.loads(self.cli(*args).stdout), archived)
        self.assertEqual(self.cli(*args, '--format', 'markdown').stdout.decode(), markdown(archived) + '\n')
        self.assertEqual(self.db.read_bytes(), before)

    def test_retained_summary_shape_failures_are_scoped_and_fail_closed(self):
        # Contracts: reject corrupt retained metrics, never reconstruct an
        # archived observation from deleted attempts or trust injected metrics.
        with closing(sqlite3.connect(self.db)) as db:
            test_id, original = db.execute(
                "SELECT test_id,data FROM observations WHERE run_id='02-ctrf' ORDER BY test_id LIMIT 1").fetchone()
        entry = json.loads(original)
        valid = report(self.db, run_ids=['04-pw'])
        cases = []
        for value in (None, {}, [], 1):
            cases.append(('retained_attempts', {**entry, 'retained_attempts': value}))
        missing = dict(entry)
        del missing['retained_attempts']
        cases.append(('retained_attempts', missing))
        retained = entry['retained_attempts']
        for field, values in (
                ('available_attempts', (True, -1, 1.5, '1', None, 2**53)),
                ('available_attempt_duration_ms', (True, -1, '1', None, float('nan'), float('inf'))),
                ('classification', (None, [], {}, {'terminal_status': 'passed'}))):
            for value in values:
                diagnostic = 'invalid number' if isinstance(value, float) and not math.isfinite(value) else field
                cases.append((diagnostic, {**entry, 'retained_attempts': {**retained, field: value}}))
        for field, value in (('terminal_status', 'unknown'), ('fail_then_pass', 1),
                             ('mapped_fail_then_pass', None), ('passed', 'false')):
            flags = {**retained['classification'], field: value}
            cases.append((field, {**entry, 'retained_attempts': {**retained, 'classification': flags}}))
        cases.append(('retained_attempts', {**entry, 'retained_attempts': {**retained, 'extra': 0}}))
        for field, changed in cases:
            with self.subTest(field=field, value=changed.get('retained_attempts')):
                self.execute("UPDATE observations SET data=? WHERE run_id='02-ctrf' AND test_id=?",
                             (json.dumps(changed), test_id))
                before = self.db.read_bytes(), self.archive.read_bytes()
                self.assertEqual(report(self.db, run_ids=['04-pw']), valid)
                with self.assertRaisesRegex(LedgerError, '02-ctrf.*' + test_id):
                    report(self.db, run_ids=['02-ctrf'])
                for format in ('json', 'markdown'):
                    failed = self.cli('--run-id', '02-ctrf', '--format', format, expected=2)
                    self.assertEqual(failed.stdout, b'')
                    self.assertIn(b'cannot summarize ledger', failed.stderr)
                    self.assertIn(field.encode(), failed.stderr)
                self.assertEqual((self.db.read_bytes(), self.archive.read_bytes()), before)
        self.execute("UPDATE observations SET data=? WHERE run_id='02-ctrf' AND test_id=?", (original, test_id))

    def test_retained_summary_state_conflicts_and_duplicate_json_fail_closed(self):
        with closing(sqlite3.connect(self.db)) as db:
            archived_id, archived = db.execute(
                "SELECT test_id,data FROM observations WHERE run_id='01-pw' ORDER BY test_id LIMIT 1").fetchone()
            full_id, full = db.execute(
                "SELECT test_id,data FROM observations WHERE run_id='04-pw' ORDER BY test_id LIMIT 1").fetchone()
        retained = json.loads(archived)['retained_attempts']
        injected = {**json.loads(full), 'retained_attempts': retained}
        self.execute("UPDATE observations SET data=? WHERE run_id='04-pw' AND test_id=?", (json.dumps(injected), full_id))
        with self.assertRaisesRegex(LedgerError, 'full observation.*retained_attempts'):
            report(self.db, run_ids=['04-pw'])
        self.execute("UPDATE observations SET data=? WHERE run_id='04-pw' AND test_id=?", (full, full_id))
        self.execute("INSERT INTO attempts SELECT '01-pw',?,attempt_index,status,duration_ms,data "
                     "FROM attempts WHERE run_id='04-pw' AND test_id=?", (archived_id, full_id))
        with self.assertRaisesRegex(LedgerError, 'archived observation.*local attempts'):
            report(self.db, run_ids=['01-pw'])
        self.execute("DELETE FROM attempts WHERE run_id='01-pw'")
        for invalid, diagnostic in (('[]', 'expected object'),
                (archived[:-1] + ',"retained_attempts":{}}', 'duplicate object key'),
                ('[' * 1100 + '0' + ']' * 1100, 'nesting')):
            with self.subTest(diagnostic=diagnostic):
                self.execute("UPDATE observations SET data=? WHERE run_id='01-pw' AND test_id=?", (invalid, archived_id))
                before = self.db.read_bytes()
                for format in ('json', 'markdown'):
                    failed = self.cli('--run-id', '01-pw', '--format', format, expected=2)
                    self.assertEqual(failed.stdout, b'')
                    self.assertIn(diagnostic.encode(), failed.stderr)
                    self.assertIn(archived_id.encode(), failed.stderr)
                self.assertEqual(self.db.read_bytes(), before)

    def test_membership_and_evidence_share_one_snapshot(self):
        from qawrench.flaky.ledger import _read_db
        ids = ['01-pw', '05-ctrf']
        expected = report(self.db, run_ids=ids)
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            def concurrent_reader(path):
                db = _read_db(path)
                with writer:
                    writer.execute('DELETE FROM attempts')
                    writer.execute('DELETE FROM observations')
                    writer.execute('DELETE FROM runs')
                return db
            with patch('qawrench.flaky.ledger._read_db', side_effect=concurrent_reader) as read:
                self.assertEqual(report(self.db, run_ids=ids), expected)
                read.assert_called_once_with(self.db)
