"""Exact selected-test sequence contracts; synthetic reports, no source replay."""
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

from qawrench.flaky import LedgerError, import_report, prune

ROOT = Path(__file__).resolve().parents[2]
CTRF = (ROOT / 'examples/flaky/ctrf.json').read_bytes()


def native(attempts, *, name='target', project='chromium', file='test.ts', empty=False):
    outcome = 'skipped' if not attempts else 'unexpected' if attempts[-1][0] == 'failed' else 'expected'
    counts = dict(expected=0, unexpected=0, flaky=0, skipped=0)
    if not empty:
        counts[outcome] = 1
    specs = [] if empty else [dict(title=name, file=file, tests=[dict(projectName=project,
        projectId=project, expectedStatus='passed', status=outcome,
        results=[dict(retry=i, status=s, duration=d) for i, (s, d) in enumerate(attempts)])])]
    return json.dumps(dict(config=dict(version='1.56.1', projects=[dict(id=project, name=project)]),
        suites=[dict(title=file, file=file, specs=specs)], errors=[],
        stats=dict(startTime='2026-01-01', duration=10, **counts))).encode()


def identity(format='playwright', project='chromium', project_id='chromium', file='test.ts', titles=None):
    return hashlib.sha256(json.dumps([format, project, project_id, file, titles or ['target']],
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def outcome(status):
    return dict(terminal_status=status, fail_then_pass=False, mapped_fail_then_pass=False,
        terminal_failure=status == 'failed', expected_failure=False, unexpected_pass=False,
        interrupted=False, skipped=False, passed=status == 'passed', not_run=status == 'not_run')


class TestHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'history.sqlite'
        for run_id, attempts in [('Fail-A', [('failed', 2), ('failed', 3)]),
                                 ('fail-b', [('failed', 4)]), ('Pass', [('passed', 7)])]:
            import_report(self.db, native(attempts), format='playwright', run_id=run_id)
        self.ids = ['Fail-A', 'fail-b', 'Pass']
        self.test_id = identity()

    def execute(self, sql, params=()):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute(sql, params)

    def history(self, ids=None, test_id=None):
        from qawrench.flaky import test_history
        return test_history(self.db, test_id=self.test_id if test_id is None else test_id,
                            run_ids=self.ids if ids is None else ids)

    def cli(self, ids=None, test_id=None, expected=0):
        command = [sys.executable, '-m', 'qawrench.flaky', 'test-history', '--db', str(self.db),
                   '--test-id', self.test_id if test_id is None else test_id]
        for run_id in self.ids if ids is None else ids:
            command.extend(['--run-id', run_id])
        result = subprocess.run(command, cwd=self.root, env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')},
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, expected, result.stderr.decode())
        self.assertNotIn(b'Traceback', result.stderr)
        if expected:
            self.assertEqual(result.stdout, b'')
            self.assertTrue(result.stderr)
        else:
            self.assertEqual(result.stderr, b'')
        return result

    def archive(self, keep=1):
        self.execute("UPDATE runs SET imported_at='2026-01-01'")
        # Case-sensitive descending IDs: fail-b sorts first, so use explicit timestamps.
        for i, run_id in enumerate(self.ids):
            self.execute('UPDATE runs SET imported_at=? WHERE run_id=?', (f'2026-01-0{i+1}', run_id))
        path = self.root / 'older.json'
        prune(self.db, keep_latest=keep, archive_path=path)
        path.unlink()

    def test_three_run_exact_contract_fail_fail_pass_in_caller_order(self):
        expected = []
        for run_id, status, count, duration in [('Fail-A', 'failed', 2, 5),
                                               ('fail-b', 'failed', 1, 4), ('Pass', 'passed', 1, 7)]:
            expected.append(dict(run_id=run_id, format='playwright', project_context=None, state='full',
                presence='present', evidence=dict(expected_status='passed', reported_status='unexpected' if status == 'failed' else 'expected',
                    reported_flaky=False, retries=count-1, reported_duration_ms=duration,
                    attempt_details_unavailable=False, status_detail_unavailable=False, outcome=outcome(status),
                    available_attempt_count=count, available_attempt_duration_ms=duration, attempts_archived=False)))
        data = dict(action='test-history', test_id=self.test_id,
            identity=dict(test_id=self.test_id, format='playwright', project='chromium', project_id='chromium',
                          file='test.ts', title_path=['target'], identity_path_opaque=False), runs=expected)
        before = self.db.read_bytes()
        first = self.cli()
        self.assertEqual(json.loads(first.stdout), data)
        self.assertEqual(self.history(), data)
        self.assertEqual(self.cli().stdout, first.stdout)
        order = ['Pass', 'Fail-A', 'Pass', 'fail-b']
        self.assertEqual(self.history(order)['runs'], [expected[2], expected[0], expected[2], expected[1]])
        self.assertEqual(self.db.read_bytes(), before)

    def test_archive_counts_missing_observation_and_observed_not_run_are_distinct(self):
        import_report(self.db, native([], empty=True), format='playwright', run_id='Empty')
        import_report(self.db, native([]), format='playwright', run_id='Not-run')
        self.archive()
        before = self.db.read_bytes()
        data = self.history(['Empty', 'Fail-A', 'Not-run', 'Pass', 'fail-b'])
        rows = data['runs']
        self.assertEqual((rows[0]['presence'], rows[0]['evidence'], rows[0]['state']), ('absent', None, 'archived'))
        self.assertEqual([r['evidence']['available_attempt_count'] for r in rows[1:]], [2, 0, 1, 1])
        self.assertTrue(rows[1]['evidence']['attempts_archived'])
        self.assertFalse(rows[1]['evidence']['attempt_details_unavailable'])
        self.assertTrue(rows[2]['evidence']['outcome']['not_run'])
        self.assertTrue(rows[2]['evidence']['attempt_details_unavailable'])
        self.assertFalse(rows[3]['evidence']['attempts_archived'])
        self.assertEqual(self.history(['Empty'])['identity'], None)
        self.assertEqual(json.loads(self.cli(['Empty']).stdout), self.history(['Empty']))
        self.assertEqual(self.db.read_bytes(), before)

    def test_ctrf_aggregate_gap_and_mapped_outcome_full_and_archived(self):
        for run_id in ('ctrf-old', 'ctrf-new'):
            import_report(self.db, CTRF, format='ctrf', project='chromium', run_id=run_id)
        self.ids.extend(['ctrf-old', 'ctrf-new'])
        self.archive(keep=1)
        with closing(sqlite3.connect(self.db)) as db:
            entries = [json.loads(row[0]) for row in db.execute("SELECT data FROM observations WHERE run_id='ctrf-new'")]
        for name in ('aggregate retries', 'detailed retries'):
            entry = next(e for e in entries if e['title_path'][-1] == name)
            data = self.history(['ctrf-old', 'ctrf-new', 'Pass'], entry['test_id'])
            self.assertTrue(data['identity']['identity_path_opaque'])
            a, b, absent = data['runs']
            self.assertEqual({**a['evidence'], 'attempts_archived': False}, b['evidence'])
            self.assertTrue(a['evidence']['attempts_archived'])
            self.assertEqual((absent['presence'], absent['evidence']), ('absent', None))
            if name == 'aggregate retries':
                self.assertEqual((a['evidence']['retries'], a['evidence']['available_attempt_count']), (2, 1))
                self.assertTrue(a['evidence']['attempt_details_unavailable'])
                self.assertFalse(a['evidence']['outcome']['fail_then_pass'])
            else:
                self.assertTrue(a['evidence']['outcome']['mapped_fail_then_pass'])

    def test_project_file_title_and_adapter_namespaces_no_inference(self):
        for run_id, kwargs in [('project', dict(project='firefox')), ('file', dict(file='renamed.ts')),
                               ('title', dict(name='renamed'))]:
            import_report(self.db, native([('passed', 1)], **kwargs), format='playwright', run_id=run_id)
        import_report(self.db, CTRF, format='ctrf', project='chromium', run_id='ctrf')
        rows = self.history(['project', 'file', 'title', 'ctrf'])['runs']
        self.assertTrue(all(r['presence'] == 'absent' and r['evidence'] is None for r in rows))
        self.execute("UPDATE observations SET data=CAST(x'80' AS TEXT) WHERE test_id=?", (self.test_id,))
        self.assertIsNone(self.history(['project'])['identity'])

    def test_unknown_and_invalid_selectors_fail_before_relevant_decoding(self):
        self.execute("UPDATE observations SET data='{' WHERE test_id=?", (self.test_id,))
        for ids, test_id, fragment in [(['Fail-A', 'pass'], self.test_id, b'unknown run-id'),
            (self.ids, '0'*64, b'unknown test-id'), ([' '], self.test_id, b'run-id'),
            (self.ids, self.test_id.upper(), b'test-id'), ([], self.test_id, b'run-id')]:
            before = self.db.read_bytes()
            self.assertIn(fragment, self.cli(ids, test_id, expected=2).stderr)
            self.assertEqual(self.db.read_bytes(), before)
        from qawrench.flaky import test_history
        for bad_ids in (None, [], 'Pass', b'Pass', [1], [''], ['\ud800']):
            with self.subTest(ids=repr(bad_ids)), patch('qawrench.flaky.history._read_db') as read:
                with self.assertRaises(LedgerError):
                    test_history(self.db, test_id=self.test_id, run_ids=bad_ids)
                read.assert_not_called()
        for bad_test in (None, 1, [], 'x', 'a'*63, 'a'*65, ' '+self.test_id, '\ud800'):
            with self.subTest(test=repr(bad_test)), patch('qawrench.flaky.history._read_db') as read:
                with self.assertRaises(LedgerError):
                    test_history(self.db, test_id=bad_test, run_ids=['Pass'])
                read.assert_not_called()
        self.db = self.root / 'missing.sqlite'
        self.cli(expected=2)
        self.assertFalse(self.db.exists())
        self.execute('CREATE TABLE unrelated(x)')
        self.assertIn(b'not a retry-history-ledger', self.cli(expected=2).stderr)

    def test_only_selected_identity_evidence_is_decoded_no_external_reads(self):
        import_report(self.db, native([('passed', 1)], name='unrelated'), format='playwright', run_id='Other')
        # Copy a different valid identity into a selected run, then corrupt all its evidence.
        self.execute("INSERT INTO observations SELECT 'Pass',test_id,data FROM observations WHERE run_id='Other'")
        self.execute("INSERT INTO attempts SELECT 'Pass',test_id,attempt_index,status,duration_ms,data FROM attempts WHERE run_id='Other'")
        self.archive()
        expected = self.history()
        self.execute("UPDATE observations SET data=CAST(x'80' AS TEXT) WHERE test_id!=?", (self.test_id,))
        self.execute("UPDATE attempts SET data=CAST(x'80' AS TEXT) WHERE test_id!=?", (self.test_id,))
        self.execute("UPDATE runs SET raw_report=?,metadata=CAST(x'80' AS TEXT),digest='bad'", (b'invalid',))
        self.execute("UPDATE runs SET archive='{' WHERE archive IS NOT NULL")
        self.execute("UPDATE runs SET format='bad' WHERE run_id='Other'")
        before = self.db.read_bytes()
        with patch('builtins.open', side_effect=AssertionError('external read')), \
             patch('pathlib.Path.open', side_effect=AssertionError('external read')), \
             patch('qawrench.flaky.adapters.parse_report', side_effect=AssertionError('replay')):
            self.assertEqual(self.history(), expected)
        self.assertEqual(json.loads(self.cli().stdout), expected)
        self.assertEqual(self.db.read_bytes(), before)

    def test_relevant_malformed_full_and_retained_metrics_fail_closed(self):
        self.archive()
        baseline = self.db.read_bytes()
        for run_id in ('Fail-A', 'Pass'):
            with closing(sqlite3.connect(self.db)) as db:
                original = db.execute('SELECT data FROM observations WHERE run_id=? AND test_id=?', (run_id, self.test_id)).fetchone()[0]
            entry = json.loads(original)
            mutations = [(key, value) for key, values in (
                ('retries', [True, -1, 0.5, 2**53, None]), ('reported_duration_ms', [True, -1, float('inf')]),
                ('reported_flaky', [1]), ('attempt_details_unavailable', [None]),
                ('status_detail_unavailable', ['false']), ('reported_status', ['bad']),
                ('expected_status', [None]), ('title_path', [[]]), ('test_id', ['0'*64])) for value in values]
            if run_id == 'Fail-A':
                for key, value in [('available_attempts', True), ('available_attempt_duration_ms', -1),
                                   ('classification', [])]:
                    mutations.append(('retained_attempts', {**entry['retained_attempts'], key: value}))
                mutations.append(('retained_attempts', {}))
            else:
                mutations.append(('retained_attempts', {}))
            for field, value in mutations:
                with self.subTest(run_id=run_id, field=field, value=value):
                    self.db.write_bytes(baseline)
                    self.execute('UPDATE observations SET data=? WHERE run_id=? AND test_id=?',
                                 (json.dumps({**entry, field: value}), run_id, self.test_id))
                    before = self.db.read_bytes()
                    error = self.cli(expected=2).stderr
                    self.assertIn(run_id.encode(), error)
                    self.assertIn(self.test_id.encode(), error)
                    self.assertEqual(self.db.read_bytes(), before)
                    self.assertEqual(self.history(['fail-b'])['runs'][0]['presence'], 'present')
        self.db.write_bytes(baseline)
        for text in ('[]', '{', '{"retries":0,"retries":1}', '['*1100+'0'+']'*1100):
            self.execute("UPDATE observations SET data=? WHERE run_id='Pass' AND test_id=?", (text, self.test_id))
            self.cli(expected=2)

    def test_full_attempt_validation_context_and_archive_state(self):
        baseline = self.db.read_bytes()
        with closing(sqlite3.connect(self.db)) as db:
            original = db.execute("SELECT data FROM attempts WHERE run_id='Pass' AND test_id=?", (self.test_id,)).fetchone()[0]
        for key, value in [('index', True), ('index', 9), ('status', 'bad'), ('duration_ms', True), ('duration_ms', -1)]:
            self.db.write_bytes(baseline)
            self.execute("UPDATE attempts SET data=? WHERE run_id='Pass' AND test_id=?", (json.dumps({**json.loads(original), key: value}), self.test_id))
            self.cli(expected=2)
        for column, value in [('status', 'failed'), ('duration_ms', 9), ('data', '[]')]:
            self.db.write_bytes(baseline)
            self.execute(f"UPDATE attempts SET {column}=? WHERE run_id='Pass'", (value,))
            self.cli(expected=2)
        for column, value in [('format', 'bad'), ('project_context', 'wrong')]:
            self.db.write_bytes(baseline)
            self.execute(f"UPDATE runs SET {column}=? WHERE run_id='Pass'", (value,))
            self.cli(expected=2)
        self.db.write_bytes(baseline)
        self.archive()
        self.execute("INSERT INTO attempts SELECT 'Fail-A',test_id,attempt_index,status,duration_ms,data FROM attempts WHERE run_id='Pass'")
        self.assertIn(b'archived observation', self.cli(expected=2).stderr)

    def test_generator_order_literal_ids_and_single_read_snapshot(self):
        literal = ' Pass \u00e9 '
        import_report(self.db, native([('passed', 1)]), format='playwright', run_id=literal)
        self.assertEqual([r['run_id'] for r in self.history(iter(['Pass', literal, 'Pass']))['runs']], ['Pass', literal, 'Pass'])
        expected = self.history(['Pass', 'Fail-A', 'Pass'])
        from qawrench.flaky.ledger import _read_db
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            def concurrent_read(path):
                db = _read_db(path)
                with writer:
                    writer.execute('DELETE FROM attempts')
                    writer.execute('DELETE FROM observations')
                    writer.execute('DELETE FROM runs')
                return db
            with patch('qawrench.flaky.history._read_db', side_effect=concurrent_read) as read:
                self.assertEqual(self.history(['Pass', 'Fail-A', 'Pass']), expected)
                read.assert_called_once_with(self.db)
