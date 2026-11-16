"""Selected report evidence requires literal booleans, without replay/audit."""

from contextlib import closing
import itertools
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
FIELDS = ('reported_flaky', 'attempt_details_unavailable', 'status_detail_unavailable')


class ReportBooleanTests(unittest.TestCase):
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

    def test_selected_integer_flags_raise_contextual_boolean_error(self):
        db, run_id = self.contexts[0]
        test_id, original = self.observation(db, run_id)
        for field in FIELDS:
            with self.subTest(field=field):
                self.store(db, run_id, test_id, json.dumps({**json.loads(original), field: 1}))
                before = db.read_bytes()
                with self.assertRaisesRegex(LedgerError, run_id + '.*' + test_id + '.*' + field + '.*expected boolean'):
                    report(db, run_ids=[run_id])
                self.assertEqual(db.read_bytes(), before)

    def test_invalid_flags_fail_closed_only_when_selected(self):
        values = (0, 1, 0.0, 1.0, 'false', 'true', '', None, [], {})
        for db, run_id in self.contexts:
            test_id, original = self.observation(db, run_id)
            valid_id = 'full-ctrf' if run_id != 'full-ctrf' else 'full-playwright'
            valid = report(db, run_ids=[valid_id])
            for field, (label, value) in itertools.product(
                    FIELDS, [('value', v) for v in values] + [('missing', None)]):
                with self.subTest(version=db.stem, run_id=run_id, field=field, label=label, value=value):
                    entry = json.loads(original)
                    if label == 'missing':
                        del entry[field]
                    else:
                        entry[field] = value
                    self.store(db, run_id, test_id, json.dumps(entry))
                    before = db.read_bytes()
                    with self.assertRaisesRegex(LedgerError, run_id + '.*' + test_id + '.*' + field + '.*expected boolean'):
                        report(db, run_ids=[valid_id, run_id])
                    for format in ('json', 'markdown'):
                        failed = self.cli(db, '--run-id', valid_id, '--run-id', run_id,
                                          '--format', format, expected=2)
                        for context in (run_id, test_id, field, 'expected boolean'):
                            self.assertIn(context.encode(), failed.stderr)
                    self.assertEqual(report(db, run_ids=[valid_id]), valid)
                    with self.assertRaisesRegex(LedgerError, 'unknown run-id'):
                        report(db, run_ids=[run_id, 'missing'])
                    self.assertEqual(report(db, run_ids=[])['runs'], [])
                    self.assertEqual(db.read_bytes(), before)
            # Exercise CLI selection isolation and unknown-ID precedence in both formats.
            for format in ('json', 'markdown'):
                expected = json.loads if format == 'json' else lambda b: b.decode()
                self.assertEqual(expected(self.cli(db, '--run-id', valid_id, '--format', format).stdout),
                                 valid if format == 'json' else markdown(valid) + '\n')
                self.assertIn(b'unknown run-id', self.cli(db, '--run-id', run_id, '--run-id',
                                                        'missing', '--format', format, expected=2).stderr)
            self.store(db, run_id, test_id, original)

    def test_true_false_preserve_evidence_and_drive_only_matching_metrics(self):
        for db, run_id in self.contexts:
            baseline = report(db, run_ids=[run_id])
            test_id, original = self.observation(db, run_id)
            old = next(o for o in baseline['runs'][0]['observations'] if o['test_id'] == test_id)
            for values in itertools.product((False, True), repeat=len(FIELDS)):
                with self.subTest(version=db.stem, run_id=run_id, flags=values):
                    flags = dict(zip(FIELDS, values))
                    self.store(db, run_id, test_id, json.dumps({**json.loads(original), **flags}))
                    before = db.read_bytes()
                    data = report(db, run_ids=[run_id])
                    observation = next(o for o in data['runs'][0]['observations'] if o['test_id'] == test_id)
                    self.assertEqual(observation, {**old, **flags})
                    for field, value in flags.items():
                        self.assertIs(observation[field], value)
                        self.assertEqual(data['totals'][field], baseline['totals'][field] - old[field] + value)
                    self.assertEqual(data['totals']['attempt_duration_complete'],
                                     data['totals']['attempt_details_unavailable'] == 0)
                    for key in baseline['totals'].keys() - set(FIELDS) - {'attempt_duration_complete'}:
                        self.assertEqual(data['totals'][key], baseline['totals'][key], key)
                    history = next(t for t in data['tests'] if t['test_id'] == test_id)
                    self.assertEqual(history['reported_flaky_runs'], [run_id] if flags['reported_flaky'] else [])
                    self.assertEqual(history['attempt_details_unavailable_runs'],
                                     [run_id] if flags['attempt_details_unavailable'] else [])
                    self.assertEqual(json.loads(self.cli(db, '--run-id', run_id).stdout), data)
                    self.assertEqual(self.cli(db, '--run-id', run_id, '--format', 'markdown').stdout.decode(),
                                     markdown(data) + '\n')
                    self.assertEqual(db.read_bytes(), before)
            self.store(db, run_id, test_id, original)
            self.assertEqual(report(db, run_ids=[run_id]), baseline)
