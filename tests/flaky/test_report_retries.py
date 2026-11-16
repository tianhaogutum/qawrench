"""Typed reported-retry counts, scoped to exact selected report evidence."""

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


class ReportRetryTests(unittest.TestCase):
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
                archive.unlink()  # Retained counts need no archive reads.

    def observation(self, db, run_id):
        with closing(sqlite3.connect(db)) as connection:
            return connection.execute('SELECT test_id,data FROM observations WHERE run_id=? '
                                      'ORDER BY test_id LIMIT 1', (run_id,)).fetchone()

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

    def test_selected_boolean_retries_raise_contextual_error(self):
        db, run_id = self.contexts[0]
        test_id, original = self.observation(db, run_id)
        entry = json.loads(original)
        entry['retries'] = True
        self.store(db, run_id, test_id, json.dumps(entry))
        before = db.read_bytes()
        with self.assertRaisesRegex(LedgerError, run_id + '.*' + test_id + '.*retries'):
            report(db, run_ids=[run_id])
        self.assertEqual(db.read_bytes(), before)

    def test_invalid_retry_counts_fail_closed_only_when_selected(self):
        # Count shape only: no replay, relation to attempts, or whole-ledger audit.
        values = (True, False, -1, 0.5, 1.0, 2**53, None, '1', [], {})
        for db, run_id in self.contexts:
            test_id, original = self.observation(db, run_id)
            valid_id = 'full-ctrf' if run_id != 'full-ctrf' else 'full-playwright'
            valid = report(db, run_ids=[valid_id])
            for label, value in [('value', v) for v in values] + [('missing', None)]:
                with self.subTest(version=db.stem, run_id=run_id, label=label, value=value):
                    entry = json.loads(original)
                    if label == 'missing':
                        del entry['retries']
                    else:
                        entry['retries'] = value
                    self.store(db, run_id, test_id, json.dumps(entry))
                    before = db.read_bytes()
                    with self.assertRaisesRegex(LedgerError, run_id + '.*' + test_id + '.*retries'):
                        report(db, run_ids=[valid_id, run_id])
                    for format in ('json', 'markdown'):
                        failed = self.cli(db, '--run-id', valid_id, '--run-id', run_id,
                                          '--format', format, expected=2)
                        for context in (run_id, test_id, 'retries'):
                            self.assertIn(context.encode(), failed.stderr)
                    self.assertEqual(report(db, run_ids=[valid_id]), valid)
                    self.assertEqual(json.loads(self.cli(db, '--run-id', valid_id).stdout), valid)
                    self.assertEqual(self.cli(db, '--run-id', valid_id, '--format', 'markdown').stdout.decode(),
                                     markdown(valid) + '\n')
                    # Unknown IDs win before decoding even malformed selected retries.
                    with self.assertRaisesRegex(LedgerError, 'unknown run-id'):
                        report(db, run_ids=[run_id, 'missing'])
                    self.assertIn(b'unknown run-id', self.cli(db, '--run-id', run_id, '--run-id',
                                                            'missing', expected=2).stderr)
                    self.assertEqual(report(db, run_ids=[])['runs'], [])
                    self.assertEqual(db.read_bytes(), before)
            self.store(db, run_id, test_id, original)

    def test_valid_integer_boundaries_preserve_available_counts_and_gaps(self):
        for db, run_id in self.contexts:
            original_report = report(db, run_ids=[run_id])
            test_id, original = self.observation(db, run_id)
            original_observation = next(o for o in original_report['runs'][0]['observations']
                                        if o['test_id'] == test_id)
            for value in (0, 1, 2**53 - 1):
                with self.subTest(version=db.stem, run_id=run_id, retries=value):
                    self.store(db, run_id, test_id, json.dumps({**json.loads(original), 'retries': value}))
                    before = db.read_bytes()
                    data = report(db, run_ids=[run_id])
                    observation = next(o for o in data['runs'][0]['observations'] if o['test_id'] == test_id)
                    self.assertIs(type(observation['retries']), int)
                    self.assertEqual(observation['retries'], value)
                    for key in original_observation.keys() - {'retries'}:
                        self.assertEqual(observation[key], original_observation[key], key)
                    self.assertEqual(data['totals']['available_attempts'], original_report['totals']['available_attempts'])
                    self.assertEqual(data['totals']['reported_retries'],
                                     original_report['totals']['reported_retries'] - json.loads(original)['retries'] + value)
                    self.assertEqual(json.loads(self.cli(db, '--run-id', run_id).stdout), data)
                    self.assertEqual(self.cli(db, '--run-id', run_id, '--format', 'markdown').stdout.decode(),
                                     markdown(data) + '\n')
                    self.assertEqual(db.read_bytes(), before)
            self.store(db, run_id, test_id, original)
            self.assertEqual(report(db, run_ids=[run_id]), original_report)
