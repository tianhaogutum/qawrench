"""Stored-evidence comparison contracts; synthetic input, never source replay."""
from contextlib import closing
import hashlib
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

from qawrench.flaky import LedgerError, import_report, prune

ROOT = Path(__file__).resolve().parents[2]
CTRF = (ROOT / 'examples/flaky/ctrf.json').read_bytes()


def native(*, current=False, empty=False, project='chromium', file='test.ts'):
    rows = [] if empty else [('shared', [('passed', 7)] if current else [('failed', 2), ('passed', 3)]),
                             ('new' if current else 'absent', [('passed', 1)] if current else [])]
    specs, counts = [], dict(expected=0, unexpected=0, flaky=0, skipped=0)
    for name, attempts in rows:
        outcome = 'skipped' if not attempts else 'flaky' if len(attempts) > 1 else 'expected'
        counts[outcome] += 1
        specs.append(dict(title=name, file=file, tests=[dict(projectName=project, projectId=project,
            expectedStatus='passed', status=outcome, results=[dict(retry=i, status=s, duration=d)
                for i, (s, d) in enumerate(attempts)])]))
    return json.dumps(dict(config=dict(version='1.56.1', projects=[dict(id=project, name=project)]),
        suites=[dict(title=file, file=file, specs=specs)], errors=[],
        stats=dict(startTime='2026-01-01', duration=10, **counts))).encode()


def identity(name, project='chromium', file='test.ts'):
    return hashlib.sha256(json.dumps(['playwright', project, project, file, [name]],
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def flags(status, **overrides):
    return dict(terminal_status=status, fail_then_pass=False, mapped_fail_then_pass=False,
        terminal_failure=False, expected_failure=False, unexpected_pass=False, interrupted=False,
        skipped=False, passed=status == 'passed', not_run=status == 'not_run') | overrides


class CompareRunsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'history.sqlite'
        for run_id, raw, format, project in (
            ('01-base', native(), 'playwright', None), ('02-ctrf', CTRF, 'ctrf', 'chromium'),
            ('03-empty', native(empty=True), 'playwright', None),
            ('04-current', native(current=True), 'playwright', None),
            ('05-ctrf', CTRF, 'ctrf', 'chromium'), ('06-empty', native(empty=True), 'playwright', None)):
            import_report(self.db, raw, format=format, project=project, run_id=run_id)
        self.execute("UPDATE runs SET imported_at='2026-01-01'")
        self.archive = self.root / 'old.json'
        prune(self.db, keep_latest=3, archive_path=self.archive)

    def execute(self, sql, params=()):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute(sql, params)

    def compare(self, base='01-base', current='04-current'):
        from qawrench.flaky import compare_runs
        return compare_runs(self.db, base_run_id=base, run_id=current)

    def cli(self, base='01-base', current='04-current', expected=0):
        result = subprocess.run([sys.executable, '-m', 'qawrench.flaky', 'compare-runs',
            '--db', str(self.db), '--base-run-id', base, '--run-id', current], cwd=self.root,
            env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')}, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, expected, result.stderr.decode())
        self.assertNotIn(b'Traceback', result.stderr)
        if expected:
            self.assertEqual(result.stdout, b'')
            self.assertTrue(result.stderr)
        else:
            self.assertEqual(result.stderr, b'')
        return result

    def test_cli_exact_native_contract_retained_counts_null_absence_and_determinism(self):
        before = self.db.read_bytes(), self.archive.read_bytes()
        first = self.cli()
        data = json.loads(first.stdout)
        expected_rows = []
        for name, membership, base, current in (
            ('shared', 'both', dict(expected_status='passed', reported_status='flaky', reported_flaky=True,
                retries=1, outcome=flags('passed', fail_then_pass=True), available_attempt_count=2,
                available_attempt_duration_ms=5, reported_duration_ms=5, attempts_archived=True,
                attempt_details_unavailable=False, status_detail_unavailable=False),
                dict(expected_status='passed', reported_status='expected', reported_flaky=False,
                retries=0, outcome=flags('passed'), available_attempt_count=1,
                available_attempt_duration_ms=7, reported_duration_ms=7, attempts_archived=False,
                attempt_details_unavailable=False, status_detail_unavailable=False)),
            ('absent', 'base_only', dict(expected_status='passed', reported_status='skipped', reported_flaky=False,
                retries=0, outcome=flags('not_run'), available_attempt_count=0,
                available_attempt_duration_ms=0, reported_duration_ms=0, attempts_archived=True,
                attempt_details_unavailable=True, status_detail_unavailable=False), None),
            ('new', 'current_only', None, dict(expected_status='passed', reported_status='expected', reported_flaky=False,
                retries=0, outcome=flags('passed'), available_attempt_count=1,
                available_attempt_duration_ms=1, reported_duration_ms=1, attempts_archived=False,
                attempt_details_unavailable=False, status_detail_unavailable=False))):
            expected_rows.append(dict(test_id=identity(name), format='playwright', project='chromium',
                project_id='chromium', file='test.ts', title_path=[name], identity_path_opaque=False,
                membership=membership, base=base, current=current))
        self.assertEqual(data, dict(action='compared', base_run_id='01-base', run_id='04-current',
            base=dict(format='playwright', project_context=None, state='archived', observation_count=2),
            current=dict(format='playwright', project_context=None, state='full', observation_count=2),
            tests=sorted(expected_rows, key=lambda r: r['test_id'])))
        self.assertEqual(self.cli().stdout, first.stdout)
        self.assertEqual(self.compare(), data)
        self.assertEqual((self.db.read_bytes(), self.archive.read_bytes()), before)

    def test_ctrf_full_and_archived_keep_counts_gaps_and_mapped_evidence(self):
        data = self.compare('02-ctrf', '05-ctrf')
        self.assertEqual(len(data['tests']), 6)
        self.assertTrue(all(row['membership'] == 'both' and row['identity_path_opaque'] for row in data['tests']))
        self.assertEqual(sum(r['base']['available_attempt_count'] for r in data['tests']), 7)
        for row in data['tests']:
            self.assertTrue(row['base']['attempts_archived'])
            self.assertFalse(row['current']['attempts_archived'])
            self.assertEqual({**row['base'], 'attempts_archived': False}, row['current'])
            if row['title_path'][-1] == 'aggregate retries':
                self.assertEqual((row['base']['retries'], row['base']['available_attempt_count'],
                                  row['base']['available_attempt_duration_ms']), (2, 1, 7))
                self.assertTrue(row['base']['attempt_details_unavailable'])
                self.assertFalse(row['base']['outcome']['fail_then_pass'])
            if row['title_path'][-1] == 'detailed retries':
                self.assertTrue(row['base']['outcome']['mapped_fail_then_pass'])
                self.assertFalse(row['base']['outcome']['fail_then_pass'])

    def test_zero_observations_same_id_and_v1_literal_ids(self):
        empty = self.compare('03-empty', '06-empty')
        self.assertEqual(empty['tests'], [])
        self.assertEqual((empty['base']['observation_count'], empty['current']['observation_count']), (0, 0))
        self.assertTrue(all(r['membership'] == 'current_only' and r['base'] is None
                            for r in self.compare('03-empty', '04-current')['tests']))
        self.assertTrue(all(r['membership'] == 'base_only' and r['current'] is None
                            for r in self.compare('01-base', '06-empty')['tests']))
        for row in self.compare('01-base', '01-base')['tests']:
            self.assertEqual(row['base'], row['current'])
            self.assertEqual(row['membership'], 'both')
        self.db = self.root / 'v1.sqlite'
        for run_id, raw in [('Same', native()), ('same', native(current=True)), (' same ', native(empty=True))]:
            import_report(self.db, raw, format='playwright', run_id=run_id)
        before = self.db.read_bytes()
        data = json.loads(self.cli('Same', 'same').stdout)
        self.assertEqual((data['base_run_id'], data['run_id']), ('Same', 'same'))
        self.assertEqual(data['base']['state'], 'full')
        self.assertEqual(json.loads(self.cli(' same ', ' same ').stdout)['tests'], [])
        self.assertEqual(self.db.read_bytes(), before)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)

    def test_adapter_project_file_and_title_namespaces_never_guessed(self):
        self.assertTrue(all(r['membership'] != 'both' for r in self.compare('01-base', '05-ctrf')['tests']))
        for run_id, kwargs in [('project', dict(project='firefox')), ('file', dict(file='renamed.ts'))]:
            import_report(self.db, native(**kwargs), format='playwright', run_id=run_id)
            rows = self.compare('01-base', run_id)['tests']
            self.assertEqual(len(rows), 4)
            self.assertTrue(all(r['membership'] != 'both' for r in rows))
        self.assertEqual(sum(r['membership'] == 'both' for r in self.compare()['tests']), 1)

    def test_unknown_invalid_ids_fail_before_evidence_and_db_open(self):
        self.execute("UPDATE observations SET data='{' WHERE run_id='01-base'")
        for base, current in [('missing', '01-base'), ('01-base', '04-CURRENT'), (' ', '04-current'), ('01-base', '')]:
            before = self.db.read_bytes()
            result = self.cli(base, current, expected=2)
            self.assertIn(b'run-id', result.stderr)
            if base == 'missing' or current == '04-CURRENT':
                self.assertIn(b'unknown', result.stderr)
            self.assertEqual(self.db.read_bytes(), before)
        from qawrench.flaky import compare_runs
        for bad in (None, 1, [], b'01-base', '\ud800'):
            for field in ('base_run_id', 'run_id'):
                with self.subTest(bad=repr(bad), field=field), patch('qawrench.flaky.comparison._read_db') as read:
                    with self.assertRaisesRegex(LedgerError, 'run-id'):
                        compare_runs(self.db, **{**dict(base_run_id='01-base', run_id='04-current'), field: bad})
                    read.assert_not_called()
        self.db = self.root / 'missing.sqlite'
        self.assertIn(b'cannot read ledger', self.cli(expected=2).stderr)
        self.assertFalse(self.db.exists())
        self.execute('CREATE TABLE other(x)')
        self.assertIn(b'not a retry-history-ledger', self.cli(expected=2).stderr)

    def test_unselected_corruption_no_replay_source_metadata_or_external_reads(self):
        expected = self.compare()
        self.archive.unlink()
        self.execute("UPDATE runs SET raw_report=?,metadata=CAST(x'80' AS TEXT),digest='bad'", (b'invalid',))
        self.execute("UPDATE runs SET archive='{' WHERE archive IS NOT NULL")
        self.execute("UPDATE observations SET data=CAST(x'80' AS TEXT) WHERE run_id='02-ctrf'")
        self.execute("UPDATE runs SET format='bad',project_context='bad' WHERE run_id='02-ctrf'")
        self.execute("UPDATE attempts SET data='{' WHERE run_id='05-ctrf'")
        before = self.db.read_bytes()
        with patch('pathlib.Path.open', side_effect=AssertionError('external read')), \
             patch('builtins.open', side_effect=AssertionError('external read')), \
             patch('qawrench.flaky.adapters.parse_report', side_effect=AssertionError('source replay')):
            self.assertEqual(self.compare(), expected)
        self.assertEqual(json.loads(self.cli().stdout), expected)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(self.archive.exists())

    def test_malformed_selected_observation_metrics_fail_closed_only_when_selected(self):
        baseline = self.db.read_bytes()
        valid = self.compare('05-ctrf', '06-empty')
        for run_id in ('01-base', '04-current'):
            with closing(sqlite3.connect(self.db)) as db:
                test_id, original = db.execute("SELECT test_id,data FROM observations WHERE run_id=? ORDER BY test_id LIMIT 1", (run_id,)).fetchone()
            entry = json.loads(original)
            mutations = [(field, value) for field, values in (
                ('retries', [True, -1, 1.5, 2**53, None]),
                ('reported_duration_ms', [True, -1, float('inf'), '1']),
                ('attempt_details_unavailable', [1]), ('status_detail_unavailable', [None]),
                ('reported_flaky', ['true']), ('reported_status', ['unknown']),
                ('expected_status', [None]), ('title_path', [[]]), ('test_id', ['0' * 64])) for value in values]
            if run_id == '01-base':
                for key, value in [('available_attempts', True), ('available_attempts', -1),
                    ('available_attempt_duration_ms', -1), ('classification', [])]:
                    retained = {**entry['retained_attempts'], key: value}
                    mutations.append(('retained_attempts', retained))
                mutations.append(('retained_attempts', {}))
            else:
                mutations.append(('retained_attempts', {}))
            for field, value in mutations:
                with self.subTest(run_id=run_id, field=field, value=value):
                    self.db.write_bytes(baseline)
                    self.execute('UPDATE observations SET data=? WHERE run_id=? AND test_id=?',
                        (json.dumps({**entry, field: value}), run_id, test_id))
                    before = self.db.read_bytes()
                    error = self.cli(expected=2).stderr
                    self.assertIn(run_id.encode(), error)
                    self.assertIn(test_id.encode(), error)
                    self.assertIn(b'invalid number' if isinstance(value, float) and not math.isfinite(value) else field.encode(), error)
                    self.assertEqual(self.compare('05-ctrf', '06-empty'), valid)
                    self.assertEqual(self.db.read_bytes(), before)
        self.db.write_bytes(baseline)
        for data in ('[]', '{', original[:-1] + ',"retries":0}', '[' * 1100 + '0' + ']' * 1100):
            self.execute("UPDATE observations SET data=? WHERE run_id='04-current'", (data,))
            before = self.db.read_bytes()
            self.cli(expected=2)
            self.assertEqual(self.db.read_bytes(), before)

    def test_malformed_full_attempt_metrics_relational_mismatch_and_archive_state(self):
        baseline = self.db.read_bytes()
        with closing(sqlite3.connect(self.db)) as db:
            test_id, original = db.execute("SELECT test_id,data FROM attempts WHERE run_id='04-current' LIMIT 1").fetchone()
        attempt = json.loads(original)
        for field, value in [('duration_ms', True), ('duration_ms', -1), ('duration_ms', float('nan')),
                             ('status', 'unknown'), ('index', True), ('index', 99)]:
            with self.subTest(field=field, value=value):
                self.db.write_bytes(baseline)
                self.execute("UPDATE attempts SET data=? WHERE run_id='04-current' AND test_id=?",
                             (json.dumps({**attempt, field: value}), test_id))
                before = self.db.read_bytes()
                error = self.cli(expected=2).stderr
                self.assertIn(test_id.encode(), error)
                self.assertIn(b'invalid number' if isinstance(value, float) and not math.isfinite(value) else field.encode(), error)
                self.assertEqual(self.db.read_bytes(), before)
        for column, value in [('status', 'failed'), ('duration_ms', 99), ('attempt_index', 9), ('data', '[]')]:
            self.db.write_bytes(baseline)
            self.execute(f"UPDATE attempts SET {column}=? WHERE run_id='04-current'", (value,))
            before = self.db.read_bytes()
            self.cli(expected=2)
            self.assertEqual(self.db.read_bytes(), before)
        self.db.write_bytes(baseline)
        self.execute("INSERT INTO attempts SELECT '01-base',test_id,attempt_index,status,duration_ms,data FROM attempts WHERE run_id='04-current'")
        self.assertIn(b'archived observation', self.cli(expected=2).stderr)

    def test_finite_large_sums_zero_available_and_ctrf_only_statuses(self):
        with closing(sqlite3.connect(self.db)) as db:
            test_id, original = db.execute("SELECT test_id,data FROM observations WHERE run_id='01-base' LIMIT 1").fetchone()
        entry = json.loads(original)
        entry['reported_duration_ms'] = 2**54
        entry['retained_attempts']['available_attempt_duration_ms'] = 2**54
        self.execute("UPDATE observations SET data=? WHERE run_id='01-base' AND test_id=?", (json.dumps(entry), test_id))
        row = next(r for r in self.compare()['tests'] if r['test_id'] == test_id)
        self.assertEqual(row['base']['available_attempt_duration_ms'], 2**54)
        for status in ('pending', 'other'):
            ctrf = json.loads(CTRF)
            test = dict(ctrf['results']['tests'][0], status=status, rawStatus=None, retries=0, flaky=False)
            ctrf['results']['tests'] = [test]
            ctrf['results']['summary'].update(tests=1, passed=0, failed=0, skipped=0, pending=0, other=0)
            ctrf['results']['summary'][status] = 1
            import_report(self.db, json.dumps(ctrf).encode(), format='ctrf', project='chromium', run_id=status)
            self.assertEqual(self.compare(status, status)['tests'][0]['base']['outcome']['terminal_status'], status)

    def test_selected_context_identity_and_one_snapshot(self):
        expected = self.compare()
        baseline = self.db.read_bytes()
        for column, value in [('format', 'unknown'), ('project_context', 'wrong')]:
            self.db.write_bytes(baseline)
            self.execute(f"UPDATE runs SET {column}=? WHERE run_id='04-current'", (value,))
            self.cli(expected=2)
        self.db.write_bytes(baseline)
        self.execute("UPDATE runs SET project_context='wrong' WHERE run_id='05-ctrf'")
        self.cli('02-ctrf', '05-ctrf', expected=2)
        self.db.write_bytes(baseline)
        from qawrench.flaky.ledger import _read_db
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            def concurrent_reader(path):
                db = _read_db(path)
                with writer:
                    writer.execute('DELETE FROM attempts')
                    writer.execute('DELETE FROM observations')
                    writer.execute('DELETE FROM runs')
                return db
            with patch('qawrench.flaky.comparison._read_db', side_effect=concurrent_reader) as read:
                self.assertEqual(self.compare(), expected)
                read.assert_called_once_with(self.db)
