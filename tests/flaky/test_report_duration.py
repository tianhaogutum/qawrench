"""Reported duration is typed selected evidence, independent of available time."""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from qawrench.flaky import LedgerError, import_report, prune, report
from qawrench.flaky.ledger import markdown

ROOT = Path(__file__).resolve().parents[2]
FIELD = 'reported_duration_ms'
MAX_NUMBER = 2**53 - 1


class ReportDurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.contexts = []
        for version in (1, 2):
            db = self.root / f'v{version}.sqlite'
            for state in (('full',) if version == 1 else ('archived', 'full')):
                for format in ('playwright', 'ctrf'):
                    run_id = f'{state}-{format}'
                    import_report(db, (ROOT / f'examples/flaky/{format}.json').read_bytes(),
                                  format=format, project='chromium' if format == 'ctrf' else None,
                                  run_id=run_id)
                    self.contexts.append((db, run_id))
            if version == 2:
                with closing(sqlite3.connect(db)) as connection, connection:
                    connection.execute("UPDATE runs SET imported_at=CASE WHEN run_id LIKE 'archived-%' "
                                       "THEN '2026-01-01' ELSE '2026-02-01' END")
                archive = self.root / 'old.json'
                prune(db, keep_latest=2, archive_path=archive)
                archive.unlink()

    def observation(self, db, run_id):
        with closing(sqlite3.connect(db)) as connection:
            rows = connection.execute('SELECT test_id,data FROM observations WHERE run_id=? '
                                      'ORDER BY test_id', (run_id,)).fetchall()
        # Prefer evidence with a real upstream gap to guard its independence.
        return next((r for r in rows if json.loads(r[1])['attempt_details_unavailable']), rows[0])

    def store(self, db, run_id, test_id, data):
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute('UPDATE observations SET data=? WHERE run_id=? AND test_id=?',
                               (data, run_id, test_id))

    def cli(self, db, *args, expected=0):
        result = subprocess.run([sys.executable, '-m', 'qawrench.flaky', 'report',
                                 '--db', str(db), *args], cwd=self.root,
                                env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')},
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, expected, result.stderr.decode())
        self.assertNotIn(b'Traceback', result.stderr)
        self.assertEqual(result.stdout if expected else result.stderr, b'')
        return result

    def test_confirmed_boolean_and_negative_durations_raise_contextual_error(self):
        for db, run_id in self.contexts:
            test_id, original = self.observation(db, run_id)
            for value in (True, -1):
                with self.subTest(version=db.stem, run_id=run_id, value=value):
                    self.store(db, run_id, test_id, json.dumps({**json.loads(original), FIELD: value}))
                    before = db.read_bytes()
                    with self.assertRaisesRegex(LedgerError, run_id + '.*' + test_id + '.*' + FIELD +
                                                '.*expected finite nonnegative number'):
                        report(db, run_ids=[run_id])
                    self.assertEqual(db.read_bytes(), before)

    def test_invalid_duration_matrix_fails_only_when_selected(self):
        values = [('value', v) for v in (True, False, -1, -0.25, None, '0', '', [], {}, 10**400)]
        values.append(('missing', None))
        for db, run_id in self.contexts:
            test_id, original = self.observation(db, run_id)
            other = 'full-ctrf' if run_id != 'full-ctrf' else 'full-playwright'
            valid = report(db, run_ids=[other])
            cases = values + ([('value', MAX_NUMBER + 1)] if run_id.endswith('ctrf') else [])
            for label, value in cases:
                with self.subTest(version=db.stem, run_id=run_id, label=label, value=value):
                    entry = json.loads(original)
                    if label == 'missing':
                        del entry[FIELD]
                    else:
                        entry[FIELD] = value
                    self.store(db, run_id, test_id, json.dumps(entry))
                    before = db.read_bytes()
                    with self.assertRaisesRegex(LedgerError, run_id + '.*' + test_id + '.*' + FIELD):
                        report(db, run_ids=[other, run_id])
                    for format in ('json', 'markdown'):
                        error = self.cli(db, '--run-id', other, '--run-id', run_id,
                                         '--format', format, expected=2).stderr
                        for context in (run_id, test_id, FIELD):
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
            self.store(db, run_id, test_id, original)

    def test_nonfinite_json_duration_is_rejected_by_existing_decoder(self):
        for db, run_id in self.contexts:
            test_id, original = self.observation(db, run_id)
            for token in ('NaN', 'Infinity', '-Infinity', '1e999'):
                with self.subTest(version=db.stem, run_id=run_id, token=token):
                    text = json.dumps({**json.loads(original), FIELD: 'DURATION_TOKEN'})
                    self.store(db, run_id, test_id, text.replace('"DURATION_TOKEN"', token))
                    before = db.read_bytes()
                    with self.assertRaisesRegex(LedgerError, run_id + '.*' + test_id + '.*invalid observation JSON'):
                        report(db, run_ids=[run_id])
                    for format in ('json', 'markdown'):
                        error = self.cli(db, '--run-id', run_id, '--format', format, expected=2).stderr
                        for context in (run_id, test_id, 'invalid observation JSON'):
                            self.assertIn(context.encode(), error)
                    self.assertEqual(db.read_bytes(), before)
            self.store(db, run_id, test_id, original)

    def test_valid_durations_change_only_reported_duration_and_its_totals(self):
        for db, run_id in self.contexts:
            baseline = report(db, run_ids=[run_id])
            test_id, original = self.observation(db, run_id)
            old = next(o for o in baseline['runs'][0]['observations'] if o['test_id'] == test_id)
            values = [0, 0.0, 0.25, MAX_NUMBER]
            if run_id.endswith('playwright'):
                values.extend([MAX_NUMBER + 1, 1e100])
            for value in values:
                with self.subTest(version=db.stem, run_id=run_id, value=value):
                    self.store(db, run_id, test_id, json.dumps({**json.loads(original), FIELD: value}))
                    before = db.read_bytes()
                    data = report(db, run_ids=[run_id])
                    observation = next(o for o in data['runs'][0]['observations'] if o['test_id'] == test_id)
                    self.assertEqual(observation, {**old, FIELD: value})
                    self.assertIs(type(observation[FIELD]), type(value))
                    total = sum(o[FIELD] for o in data['runs'][0]['observations'])
                    expected = json.loads(json.dumps(baseline))
                    next(o for o in expected['runs'][0]['observations'] if o['test_id'] == test_id)[FIELD] = value
                    expected['runs'][0]['totals']['reported_test_duration_ms'] = total
                    expected['totals']['reported_test_duration_ms'] = total
                    self.assertEqual(data, expected)
                    self.assertEqual(json.loads(self.cli(db, '--run-id', run_id).stdout), data)
                    self.assertEqual(self.cli(db, '--run-id', run_id, '--format', 'markdown').stdout.decode(),
                                     markdown(data) + '\n')
                    self.assertEqual(db.read_bytes(), before)
            self.store(db, run_id, test_id, original)
            self.assertEqual(report(db, run_ids=[run_id]), baseline)

    def test_imported_native_sum_above_per_attempt_limit_survives_archive(self):
        from .test_ledger import PW, pw_tests
        source = json.loads(PW)
        for attempt in pw_tests(source)[0]['tests'][0]['results']:
            attempt['duration'] = MAX_NUMBER
        db = self.root / 'native-sum.sqlite'
        import_report(db, json.dumps(source).encode(), format='playwright', run_id='sum')
        for archived in (False, True):
            if archived:
                archive = self.root / 'sum.json'
                prune(db, keep_latest=0, archive_path=archive)
                archive.unlink()
            before = db.read_bytes()
            data = report(db, run_ids=['sum'])
            large = next(o for o in data['runs'][0]['observations'] if o[FIELD] > MAX_NUMBER)
            self.assertEqual(large[FIELD], MAX_NUMBER * 2)
            self.assertEqual(large['available_attempt_duration_ms'], MAX_NUMBER * 2)
            self.assertEqual(large.get('attempts_archived', False), archived)
            self.assertEqual(json.loads(self.cli(db, '--run-id', 'sum').stdout), data)
            self.assertEqual(self.cli(db, '--run-id', 'sum', '--format', 'markdown').stdout.decode(),
                             markdown(data) + '\n')
            self.assertEqual(db.read_bytes(), before)
