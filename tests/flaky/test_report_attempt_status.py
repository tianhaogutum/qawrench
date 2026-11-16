"""Selected local statuses must be normalized before outcome classification."""

from contextlib import closing
import json
import sqlite3
import unittest
from unittest.mock import patch

from qawrench.flaky import LedgerError, report
from qawrench.flaky import ledger
from qawrench.flaky.ledger import markdown
from . import test_report_attempt_duration as fixtures


class ReportAttemptStatusTests(unittest.TestCase):
    setUp = fixtures.ReportAttemptDurationTests.setUp
    cli = fixtures.ReportAttemptDurationTests.cli
    store = fixtures.ReportAttemptDurationTests.store
    full_contexts = fixtures.ReportAttemptDurationTests.full_contexts

    def retry_attempts(self, db, run_id):
        with closing(sqlite3.connect(db)) as connection:
            rows = connection.execute('SELECT test_id,attempt_index,data FROM attempts '
                'WHERE run_id=? ORDER BY test_id,attempt_index', (run_id,)).fetchall()
        test_id = next(t for t, _, _ in rows if sum(r[0] == t for r in rows) > 1)
        return [row for row in rows if row[0] == test_id]

    def test_confirmed_unsupported_terminal_status_rejected_before_classification(self):
        for db, run_id in self.full_contexts():
            test_id, index, original = self.retry_attempts(db, run_id)[-1]
            with self.subTest(version=db.stem, run_id=run_id):
                self.store(db, run_id, test_id, index,
                           json.dumps({**json.loads(original), 'status': 'unsupported-status'}))
                before = db.read_bytes()
                with patch.object(ledger, '_classify', wraps=ledger._classify) as classify:
                    with self.assertRaisesRegex(LedgerError,
                            run_id + '.*' + test_id + f'.*attempt {index}.*status'):
                        report(db, run_ids=[run_id])
                    self.assertFalse(any(call.args[0]['test_id'] == test_id
                                         for call in classify.call_args_list))
                self.assertEqual(db.read_bytes(), before)

    def test_invalid_statuses_in_prior_and_terminal_attempts_are_selected_and_read_only(self):
        for db, run_id in self.full_contexts():
            other = 'full-ctrf' if run_id != 'full-ctrf' else 'full-playwright'
            valid = report(db, run_ids=[other])
            cases = [('value', value) for value in
                     (None, True, False, 0, 1.5, [], {}, '', ' \t\n', 'unsupported-status',
                      'Passed', ' passed ', 'not_run')]
            cases += [('missing', None)]
            if run_id.endswith('playwright'):
                cases += [('value', 'pending'), ('value', 'other')]
            for test_id, index, original in self.retry_attempts(db, run_id):
                for label, value in cases:
                    with self.subTest(version=db.stem, run_id=run_id, index=index, label=label, value=value):
                        attempt = json.loads(original)
                        # Diagnostics use the relational index, even if JSON differs.
                        attempt['index'] = 987
                        if label == 'missing':
                            del attempt['status']
                        else:
                            attempt['status'] = value
                        self.store(db, run_id, test_id, index, json.dumps(attempt))
                        before = db.read_bytes()
                        with self.assertRaisesRegex(LedgerError,
                                run_id + '.*' + test_id + f'.*attempt {index}.*status'):
                            report(db, run_ids=[other, run_id])
                        for format in ('json', 'markdown'):
                            error = self.cli(db, '--run-id', other, '--run-id', run_id,
                                             '--format', format, expected=2).stderr
                            for context in (run_id, test_id, f'attempt {index}', '.status'):
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

    def test_supported_statuses_and_existing_classifications_survive(self):
        for db, run_id in self.full_contexts():
            baseline = report(db, run_ids=[run_id])
            native = run_id.endswith('playwright')
            totals = baseline['totals']
            self.assertEqual(totals['fail_then_pass'], int(native))
            self.assertEqual(totals['mapped_fail_then_pass'], int(not native))
            self.assertEqual(totals['expected_failure'], 1)
            self.assertEqual(totals['interrupted'], 1)
            self.assertEqual(totals['not_run'], int(native))
            test_id, index, original = self.retry_attempts(db, run_id)[-1]
            statuses = ['passed', 'failed', 'timedOut', 'skipped', 'interrupted']
            if not native:
                statuses += ['pending', 'other']
            for status in statuses:
                with self.subTest(version=db.stem, run_id=run_id, status=status):
                    self.store(db, run_id, test_id, index,
                               json.dumps({**json.loads(original), 'status': status}))
                    before = db.read_bytes()
                    data = report(db, run_ids=[run_id])
                    observation = next(o for o in data['runs'][0]['observations'] if o['test_id'] == test_id)
                    self.assertEqual(observation['terminal_status'], status)
                    self.assertEqual(observation['attempts'][-1]['status'], status)
                    self.assertEqual(json.loads(self.cli(db, '--run-id', run_id).stdout), data)
                    self.assertEqual(self.cli(db, '--run-id', run_id, '--format', 'markdown').stdout.decode(),
                                     markdown(data) + '\n')
                    self.assertEqual(db.read_bytes(), before)
            self.store(db, run_id, test_id, index, original)
            self.assertEqual(report(db, run_ids=[run_id]), baseline)
