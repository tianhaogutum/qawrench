"""Selected local attempt durations have the same numeric boundary as comparison."""

from contextlib import closing
import json
import sqlite3
import unittest

from qawrench.flaky import LedgerError, report
from qawrench.flaky.ledger import markdown
from . import test_report_duration as fixtures

MAX_NUMBER = fixtures.MAX_NUMBER


class ReportAttemptDurationTests(unittest.TestCase):
    setUp = fixtures.ReportDurationTests.setUp
    cli = fixtures.ReportDurationTests.cli

    def full_contexts(self):
        return [(db, run_id) for db, run_id in self.contexts if run_id.startswith('full-')]

    def attempt(self, db, run_id):
        with closing(sqlite3.connect(db)) as connection:
            return connection.execute('SELECT test_id,attempt_index,data FROM attempts '
                'WHERE run_id=? ORDER BY test_id,attempt_index LIMIT 1', (run_id,)).fetchone()

    def store(self, db, run_id, test_id, index, text):
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute('UPDATE attempts SET data=? WHERE run_id=? AND test_id=? AND attempt_index=?',
                               (text, run_id, test_id, index))

    def test_confirmed_boolean_and_negative_attempt_durations_raise_contextual_error(self):
        for db, run_id in self.full_contexts():
            test_id, index, original = self.attempt(db, run_id)
            for value in (True, -1):
                with self.subTest(version=db.stem, run_id=run_id, value=value):
                    self.store(db, run_id, test_id, index,
                               json.dumps({**json.loads(original), 'duration_ms': value}))
                    before = db.read_bytes()
                    with self.assertRaisesRegex(LedgerError,
                            run_id + '.*' + test_id + f'.*attempt {index}.*duration_ms'):
                        report(db, run_ids=[run_id])
                    self.assertEqual(db.read_bytes(), before)

    def test_invalid_duration_matrix_is_selected_contextual_and_read_only(self):
        for db, run_id in self.full_contexts():
            test_id, index, original = self.attempt(db, run_id)
            other = 'full-ctrf' if run_id != 'full-ctrf' else 'full-playwright'
            valid = report(db, run_ids=[other])
            invalid = []
            for value in (True, False, -1, -0.25, None, '0', '', [], {}, MAX_NUMBER + 1, 10**400):
                invalid.append(json.dumps({**json.loads(original), 'duration_ms': value}))
            missing = json.loads(original)
            del missing['duration_ms']
            invalid.append(json.dumps(missing))
            for token in ('NaN', 'Infinity', '-Infinity', '1e999'):
                invalid.append(json.dumps({**json.loads(original), 'duration_ms': 'TOKEN'}).replace('"TOKEN"', token))
            invalid.extend(['{', 'null', '[]',
                            original[:-1] + ',"duration_ms":1}', '[' * 2000 + ']' * 2000])
            for text in invalid:
                with self.subTest(version=db.stem, run_id=run_id, text=text[:150]):
                    self.store(db, run_id, test_id, index, text)
                    before = db.read_bytes()
                    with self.assertRaisesRegex(LedgerError,
                            run_id + '.*' + test_id + f'.*attempt {index}.*duration_ms'):
                        report(db, run_ids=[other, run_id])
                    for format in ('json', 'markdown'):
                        error = self.cli(db, '--run-id', other, '--run-id', run_id,
                                         '--format', format, expected=2).stderr
                        for context in (run_id, test_id, f'attempt {index}', 'duration_ms'):
                            self.assertIn(context.encode(), error)
                    self.assertEqual(report(db, run_ids=[other]), valid)
                    with self.assertRaisesRegex(LedgerError, 'unknown run-id'):
                        report(db, run_ids=[run_id, 'missing'])
                    self.assertEqual(report(db, run_ids=[])['runs'], [])
                    self.assertEqual(db.read_bytes(), before)
            for format in ('json', 'markdown'):
                output = self.cli(db, '--run-id', other, '--format', format).stdout
                self.assertEqual(json.loads(output) if format == 'json' else output.decode(),
                                 valid if format == 'json' else markdown(valid) + '\n')
                self.assertIn(b'unknown run-id', self.cli(db, '--run-id', run_id, '--run-id',
                    'missing', '--format', format, expected=2).stderr)
            self.store(db, run_id, test_id, index, original)

    def test_valid_values_preserve_reported_duration_and_all_other_evidence(self):
        for db, run_id in self.full_contexts():
            baseline = report(db, run_ids=[run_id])
            test_id, index, original = self.attempt(db, run_id)
            old_duration = json.loads(original)['duration_ms']
            for value in (0, 0.0, 0.25, MAX_NUMBER):
                with self.subTest(version=db.stem, run_id=run_id, value=value):
                    self.store(db, run_id, test_id, index,
                               json.dumps({**json.loads(original), 'duration_ms': value}))
                    before = db.read_bytes()
                    data = report(db, run_ids=[run_id])
                    expected = json.loads(json.dumps(baseline))
                    observation = next(o for o in expected['runs'][0]['observations'] if o['test_id'] == test_id)
                    next(a for a in observation['attempts'] if a['index'] == index)['duration_ms'] = value
                    observation['available_attempt_duration_ms'] = sum(a['duration_ms'] for a in observation['attempts'])
                    for totals in (expected['totals'], expected['runs'][0]['totals']):
                        totals['available_attempt_duration_ms'] += value - old_duration
                    self.assertEqual(data, expected)
                    actual = next(o for o in data['runs'][0]['observations'] if o['test_id'] == test_id)
                    self.assertIs(type(next(a for a in actual['attempts'] if a['index'] == index)['duration_ms']), type(value))
                    self.assertEqual(json.loads(self.cli(db, '--run-id', run_id).stdout), data)
                    self.assertEqual(self.cli(db, '--run-id', run_id, '--format', 'markdown').stdout.decode(),
                                     markdown(data) + '\n')
                    self.assertEqual(db.read_bytes(), before)
            self.store(db, run_id, test_id, index, original)
            self.assertEqual(report(db, run_ids=[run_id]), baseline)

    def test_archived_summaries_and_upstream_gaps_do_not_need_local_attempts(self):
        for db, run_id in self.contexts:
            before = db.read_bytes()
            data = report(db, run_ids=[run_id])
            self.assertTrue(any(o['attempt_details_unavailable'] for o in data['runs'][0]['observations']))
            if run_id.startswith('archived-'):
                self.assertTrue(all(o['attempts_archived'] and o['attempts'] == []
                                    for o in data['runs'][0]['observations']))
            self.assertEqual(json.loads(self.cli(db, '--run-id', run_id).stdout), data)
            self.assertEqual(self.cli(db, '--run-id', run_id, '--format', 'markdown').stdout.decode(),
                             markdown(data) + '\n')
            self.assertEqual(db.read_bytes(), before)
