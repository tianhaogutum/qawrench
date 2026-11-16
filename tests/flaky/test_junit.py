"""Selected retained outcomes as XML; no runner or source replay involved."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from qawrench.flaky import LedgerError, import_report, prune, report
from qawrench.flaky.ledger import markdown

ROOT = Path(__file__).resolve().parents[2]
CTRF = (ROOT / 'examples/flaky/ctrf.json').read_bytes()
SPECIAL = ' <&"\' Ω\n'


def native(*, empty=False, special=False):
    rows = [('passed', ['passed'], 'passed'), ('failed', ['failed'], 'passed'),
            ('timeout', ['timedOut'], 'passed'), ('interrupted', ['interrupted'], 'passed'),
            ('skipped', ['skipped'], 'passed'), ('unobserved', [], 'passed'),
            ('flaky', ['failed', 'passed'], 'passed'), ('expected failure', ['failed'], 'failed'),
            ('unexpected pass', ['passed'], 'failed')]
    counts = dict(expected=0, unexpected=0, flaky=0, skipped=0)
    specs = []
    project = 'chromium' + (SPECIAL if special else '')
    for name, statuses, expected in ([] if empty else rows):
        outcome = ('skipped' if not statuses or statuses[-1] == 'skipped' else
                   'flaky' if len(statuses) > 1 else 'expected' if statuses[-1] == expected else 'unexpected')
        counts[outcome] += 1
        specs.append(dict(title=name + (SPECIAL if special else ''), file='test.ts' + (SPECIAL if special else ''),
            tests=[dict(projectName=project, projectId=project, expectedStatus=expected, status=outcome,
                results=[dict(retry=i, status=s, duration=2.5 + i) for i, s in enumerate(statuses)])]))
    return json.dumps(dict(config=dict(version='1.56.1', projects=[dict(id=project, name=project)]),
        suites=[dict(title='test.ts', file='test.ts', specs=specs)], errors=[],
        stats=dict(startTime='2026-01-01', duration=30, **counts))).encode()


def properties(element):
    return {p.get('name'): p.get('value') for p in element.findall('./properties/property')}


def cases(suite):
    return {json.loads(properties(c)['title_path'])[-1]: c for c in suite.findall('testcase')}


class JunitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'history.sqlite'
        import_report(self.db, native(), format='playwright', run_id='selected')

    def execute(self, sql, params=()):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute(sql, params)

    def cli(self, *args, expected=0, format='junit'):
        result = subprocess.run([sys.executable, '-m', 'qawrench.flaky', 'report',
            '--db', str(self.db), '--format', format, *args], cwd=self.root,
            env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')}, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, expected, result.stderr.decode())
        self.assertNotIn(b'Traceback', result.stderr)
        if expected:
            self.assertEqual(result.stdout, b'')
            self.assertTrue(result.stderr)
        else:
            self.assertEqual(result.stderr, b'')
        return result

    def xml(self, run_id='selected'):
        from qawrench.flaky import junit_report
        return junit_report(self.db, run_id=run_id)

    def test_selected_run_xml_fixture_and_deterministic_counts(self):
        before = self.db.read_bytes()
        output = self.cli('--run-id', 'selected').stdout
        root = ET.fromstring(output)
        self.assertEqual(root.tag, 'testsuites')
        suite, = root.findall('testsuite')
        fixture = ET.parse(ROOT / 'tests/flaky/fixtures/junit_selected.xml').getroot()
        for key, value in fixture.attrib.items():
            self.assertEqual(suite.get(key), value)
            self.assertEqual(root.get(key), value)
        actual = cases(suite)
        self.assertEqual(set(actual), {c.get('name') for c in fixture})
        for expected in fixture:
            case = actual[expected.get('name')]
            states = [(c.tag, c.get('type')) for c in case if c.tag != 'properties']
            self.assertEqual(states, [(c.tag, c.get('type')) for c in expected])
            self.assertEqual(json.loads(case.get('classname')), ['playwright', 'chromium', 'chromium', 'test.ts'])
        self.assertEqual(actual['flaky'].get('time'), '0.0060')
        self.assertNotIn('time', actual['unobserved'].attrib)
        self.assertEqual(properties(actual['flaky'])['fail_then_pass'], 'true')
        self.assertEqual(properties(actual['flaky'])['retries'], '1')
        self.assertEqual(properties(actual['flaky'])['available_attempt_count'], '2')
        ids = [properties(c)['test_id'] for c in suite.findall('testcase')]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(output, self.cli('--run-id', 'selected').stdout)
        self.assertEqual(output.decode(), self.xml() + '\n')
        self.assertEqual(self.db.read_bytes(), before)

    def test_archive_and_upstream_gaps_no_attempt_cases_or_archive_reads(self):
        for run_id in ('ctrf-old', 'ctrf-full'):
            import_report(self.db, CTRF, format='ctrf', project='chromium', run_id=run_id)
        for i, run_id in enumerate(('selected', 'ctrf-old', 'ctrf-full')):
            self.execute('UPDATE runs SET imported_at=? WHERE run_id=?', (f'2026-01-0{i+1}', run_id))
        full = self.xml()
        archive = self.root / 'old.json'
        prune(self.db, keep_latest=1, archive_path=archive)
        archive.unlink()
        with patch('pathlib.Path.open', side_effect=AssertionError('external read')), \
             patch('builtins.open', side_effect=AssertionError('external read')), \
             patch('qawrench.flaky.adapters.parse_report', side_effect=AssertionError('replay')):
            archived = self.xml()
        a, b = ET.fromstring(full).find('testsuite'), ET.fromstring(archived).find('testsuite')
        self.assertEqual(a.attrib, b.attrib)
        for name, case in cases(b).items():
            old = cases(a)[name]
            self.assertEqual(case.attrib, old.attrib)
            self.assertEqual(properties(case), {**properties(old), 'attempts_archived': 'true'})
        old = cases(ET.fromstring(self.xml('ctrf-old')).find('testsuite'))
        new = cases(ET.fromstring(self.xml('ctrf-full')).find('testsuite'))
        self.assertEqual(len(old), 6)
        for name, case in old.items():
            self.assertEqual(properties(case), {**properties(new[name]), 'attempts_archived': 'true'})
        aggregate = properties(old['aggregate retries'])
        self.assertEqual((aggregate['retries'], aggregate['available_attempt_count'], aggregate['attempt_details_unavailable']), ('2', '1', 'true'))
        self.assertIsNone(old['aggregate retries'].find('failure'))
        self.assertEqual(properties(old['detailed retries'])['mapped_fail_then_pass'], 'true')

    def test_escaping_names_properties_and_namespace_identity(self):
        run_id = 'literal' + SPECIAL
        import_report(self.db, native(special=True), format='playwright', run_id=run_id)
        output = self.cli('--run-id', run_id).stdout
        suite = ET.fromstring(output).find('testsuite')
        self.assertEqual(suite.get('name'), run_id)
        self.assertIn(b'&amp;', output)
        self.assertIn(b'&lt;', output)
        self.assertIn(b'&quot;', output)
        actual = cases(suite)
        self.assertIn('passed' + SPECIAL, actual)
        for case in actual.values():
            self.assertEqual(json.loads(case.get('classname')), ['playwright', 'chromium' + SPECIAL, 'chromium' + SPECIAL, 'test.ts' + SPECIAL])
            self.assertEqual(json.loads(case.get('name')), json.loads(properties(case)['title_path']))
        # Same name/file in a distinct project remains a distinct case.
        raw = json.loads(native())
        raw['config']['projects'].append(dict(id='firefox', name='firefox'))
        for spec in raw['suites'][0]['specs']:
            spec['tests'].append({**spec['tests'][0], 'projectId': 'firefox', 'projectName': 'firefox'})
        for key in ('expected', 'unexpected', 'flaky', 'skipped'):
            raw['stats'][key] *= 2
        import_report(self.db, json.dumps(raw).encode(), format='playwright', run_id='two-projects')
        rows = ET.fromstring(self.xml('two-projects')).findall('./testsuite/testcase')
        self.assertEqual(len(rows), 18)
        self.assertEqual(len({(c.get('classname'), c.get('name')) for c in rows}), 18)

    def test_missing_unknown_repeated_invalid_ids_and_database_fail_closed(self):
        self.execute("UPDATE observations SET data='{' ")
        for args, diagnostic in [((), b'exactly one'), (('--run-id', 'selected', '--run-id', 'selected'), b'exactly one'),
                                 (('--run-id', 'Selected'), b'unknown run-id'), (('--run-id', ' '), b'run-id')]:
            before = self.db.read_bytes()
            self.assertIn(diagnostic, self.cli(*args, expected=2).stderr)
            self.assertEqual(self.db.read_bytes(), before)
        from qawrench.flaky import junit_report
        for bad in (None, 1, [], b'selected', '\ud800'):
            with patch('qawrench.flaky.junit._read_db') as read:
                with self.assertRaises(LedgerError):
                    junit_report(self.db, run_id=bad)
                read.assert_not_called()
        self.db = self.root / 'missing.sqlite'
        self.assertIn(b'cannot read ledger', self.cli('--run-id', 'selected', expected=2).stderr)
        self.assertFalse(self.db.exists())
        self.execute('CREATE TABLE other(x)')
        self.assertIn(b'not a retry-history-ledger', self.cli('--run-id', 'selected', expected=2).stderr)

    def test_empty_runs_and_unestablished_terminal_outcomes_are_not_passes(self):
        import_report(self.db, native(empty=True), format='playwright', run_id='empty')
        root = ET.fromstring(self.xml('empty'))
        self.assertEqual(root.get('tests'), '0')
        self.assertEqual(len(root.findall('testsuite')), 1)
        self.assertEqual(root.findall('./testsuite/testcase'), [])
        raw = json.loads(CTRF)
        raw['results']['tests'] = [dict(name=s, suite='suite', filePath='test.ts', status=s, duration=0, retries=0, flaky=False)
                                  for s in ('pending', 'other')]
        raw['results']['summary'].update(tests=2, passed=0, failed=0, skipped=0, pending=1, other=1)
        import_report(self.db, json.dumps(raw).encode(), format='ctrf', project='chromium', run_id='ambiguous')
        suite = ET.fromstring(self.xml('ambiguous')).find('testsuite')
        self.assertEqual(suite.get('skipped'), '2')
        self.assertTrue(all(c.find('skipped').get('type') == 'unavailable' for c in suite.findall('testcase')))
        # Even an archived 'passed' label cannot establish a pass with no available observation.
        self.execute("UPDATE runs SET imported_at='2026-01-01'")
        prune(self.db, keep_latest=0, archive_path=self.root / 'old.json')
        with closing(sqlite3.connect(self.db)) as db:
            tid, text = db.execute("SELECT test_id,data FROM observations WHERE run_id='selected' ORDER BY test_id LIMIT 1").fetchone()
        entry = json.loads(text)
        entry['retained_attempts']['available_attempts'] = 0
        entry['retained_attempts']['classification']['terminal_status'] = 'passed'
        entry['retained_attempts']['classification']['passed'] = True
        self.execute("UPDATE observations SET data=? WHERE run_id='selected' AND test_id=?", (json.dumps(entry), tid))
        case = next(c for c in ET.fromstring(self.xml()).findall('./testsuite/testcase') if properties(c)['test_id'] == tid)
        self.assertEqual(case.find('skipped').get('type'), 'unavailable')
        self.assertNotIn('time', case.attrib)

    def test_missing_final_attempt_never_promotes_a_prior_pass(self):
        raw = json.loads(native())
        target = raw['suites'][0]['specs'][6]['tests'][0]
        target['results'][0]['status'] = 'passed'
        target['results'][1]['status'] = 'failed'
        target['status'] = 'unexpected'
        raw['stats']['flaky'] -= 1
        raw['stats']['unexpected'] += 1
        import_report(self.db, json.dumps(raw).encode(), format='playwright', run_id='missing-final')
        with closing(sqlite3.connect(self.db)) as db:
            tid = next(t for t, data in db.execute("SELECT test_id,data FROM observations WHERE run_id='missing-final'")
                       if json.loads(data)['title_path'] == ['flaky'])
        self.execute("DELETE FROM attempts WHERE run_id='missing-final' AND test_id=? AND attempt_index=1", (tid,))
        case = cases(ET.fromstring(self.xml('missing-final')).find('testsuite'))['flaky']
        self.assertEqual(case.find('skipped').get('type'), 'unavailable')
        self.assertEqual(properties(case)['terminal_observation_available'], 'false')

    def test_selected_corruption_context_empty_stdout_and_unrelated_isolation(self):
        import_report(self.db, CTRF, format='ctrf', project='chromium', run_id='unrelated')
        valid = self.xml()
        self.execute("UPDATE observations SET data=CAST(x'80' AS TEXT) WHERE run_id='unrelated'")
        self.execute("UPDATE runs SET raw_report=?,metadata=CAST(x'80' AS TEXT),digest='bad'", (b'bad',))
        self.assertEqual(self.xml(), valid)
        with closing(sqlite3.connect(self.db)) as db:
            tid, text = db.execute("SELECT test_id,data FROM observations WHERE run_id='selected' ORDER BY test_id LIMIT 1").fetchone()
        entry = json.loads(text)
        for changed, field in [('{', 'observation'), (text[:-1] + ',"retries":0}', 'duplicate'),
                               (json.dumps({**entry, 'reported_duration_ms': -1}), 'reported_duration_ms'),
                               (json.dumps({**entry, 'reported_status': 'unknown'}), 'reported_status'),
                               (json.dumps({**entry, 'file': 'wrong'}), 'test_id')]:
            with self.subTest(field=field):
                self.execute("UPDATE observations SET data=? WHERE run_id='selected' AND test_id=?", (changed, tid))
                before = self.db.read_bytes()
                result = self.cli('--run-id', 'selected', expected=2)
                for part in ('selected', tid, field):
                    self.assertIn(part.encode(), result.stderr)
                self.assertEqual(self.db.read_bytes(), before)
        self.execute("UPDATE observations SET data=? WHERE run_id='selected' AND test_id=?", (text, tid))
        self.execute("UPDATE attempts SET duration_ms=99 WHERE run_id='selected'")
        self.assertIn(b'relational/JSON mismatch', self.cli('--run-id', 'selected', expected=2).stderr)

    def test_invalid_xml_characters_fail_buffered_without_lossy_replacement(self):
        run_id = 'bad\x01<&'
        import_report(self.db, native(), format='playwright', run_id=run_id)
        result = self.cli('--run-id', run_id, expected=2)
        self.assertIn(b'XML 1.0', result.stderr)
        # Adapters permit controls in identity strings; the XML boundary must reject them too.
        raw = native(special=True).replace(b'\\n', b'\\u0001')
        import_report(self.db, raw, format='playwright', run_id='bad-title')
        self.assertIn(b'XML 1.0', self.cli('--run-id', 'bad-title', expected=2).stderr)

    def test_selected_archived_malformed_summary_fails_only_when_selected(self):
        import_report(self.db, native(), format='playwright', run_id='other')
        self.execute("UPDATE runs SET imported_at='2026-01-01'")
        prune(self.db, keep_latest=0, archive_path=self.root / 'old.json')
        valid = self.xml('other')
        with closing(sqlite3.connect(self.db)) as db:
            tid, text = db.execute("SELECT test_id,data FROM observations WHERE run_id='selected' ORDER BY test_id LIMIT 1").fetchone()
        entry = json.loads(text)
        retained = entry['retained_attempts']
        for changed, diagnostic in [({**entry, 'retained_attempts': None}, 'retained_attempts'),
                ({**entry, 'retained_attempts': {**retained, 'available_attempts': True}}, 'available_attempts'),
                ({**entry, 'retained_attempts': {**retained, 'available_attempt_duration_ms': -1}}, 'available_attempt_duration_ms'),
                ({**entry, 'retained_attempts': {**retained, 'classification': {}}}, 'classification')]:
            self.execute("UPDATE observations SET data=? WHERE run_id='selected' AND test_id=?", (json.dumps(changed), tid))
            before = self.db.read_bytes()
            self.assertEqual(self.xml('other'), valid)
            result = self.cli('--run-id', 'selected', expected=2)
            for part in ('selected', tid, diagnostic):
                self.assertIn(part.encode(), result.stderr)
            self.assertEqual(self.db.read_bytes(), before)

    def test_duration_uses_available_milliseconds_and_exact_decimal_seconds(self):
        from decimal import localcontext
        self.execute("UPDATE runs SET imported_at='2026-01-01'")
        prune(self.db, keep_latest=0, archive_path=self.root / 'old.json')
        with closing(sqlite3.connect(self.db)) as db:
            tid, text = db.execute("SELECT test_id,data FROM observations WHERE run_id='selected' ORDER BY test_id LIMIT 1").fetchone()
        entry = json.loads(text)
        entry['reported_duration_ms'] = 900
        for milliseconds, seconds in [(0, '0.000'), (0.125, '0.000125'),
                (10**40 + 1, '10000000000000000000000000000000000000.001')]:
            entry['retained_attempts'].update(available_attempts=1, available_attempt_duration_ms=milliseconds)
            self.execute("UPDATE observations SET data=? WHERE run_id='selected' AND test_id=?", (json.dumps(entry), tid))
            with localcontext() as ctx:
                ctx.prec = 2
                rows = ET.fromstring(self.xml()).findall('./testsuite/testcase')
            case = next(c for c in rows if properties(c)['test_id'] == tid)
            self.assertEqual(case.get('time'), seconds)
            self.assertEqual(properties(case)['reported_duration_ms'], '900')

    def test_membership_and_evidence_use_exactly_one_readonly_snapshot(self):
        from qawrench.flaky.ledger import _read_db
        expected = self.xml()
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            def concurrent_reader(path):
                db = _read_db(path)
                with writer:
                    writer.execute('DELETE FROM attempts')
                    writer.execute('DELETE FROM observations')
                    writer.execute('DELETE FROM runs')
                return db
            with patch('qawrench.flaky.junit._read_db', side_effect=concurrent_reader) as read:
                self.assertEqual(self.xml(), expected)
                read.assert_called_once_with(self.db)

    def test_json_markdown_reports_keep_existing_output(self):
        selected = report(self.db, run_ids=['selected'])
        self.assertEqual(self.cli('--run-id', 'selected', format='json').stdout.decode(), json.dumps(selected, indent=2, sort_keys=True, allow_nan=False) + '\n')
        self.assertEqual(self.cli('--run-id', 'selected', format='markdown').stdout.decode(), markdown(selected) + '\n')
