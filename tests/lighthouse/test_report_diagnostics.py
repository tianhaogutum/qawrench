"""Concise diagnostics preserve report errors and source warning occurrences."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from examples.lighthouse.fixture_factory import AUDIT, lhr, policy
from qawrench.lighthouse import compare, create_baseline
from qawrench.lighthouse.report import markdown


def cell(value):
    return str(value).replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')


class ConciseDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.b, self.c = self.root / 'b', self.root / 'c'
        self.b.mkdir()
        self.c.mkdir()
        self.p = policy()
        self.p['metrics'][AUDIT]['relative_tolerance'] = 0

    def add(self, directory, name, *, value=2000, cpu=4, device='mobile'):
        doc = lhr(value)
        doc['configSettings']['formFactor'] = device
        doc['configSettings']['throttling']['cpuSlowdownMultiplier'] = cpu
        for i in range(7):
            (directory / f'{name}-{i}.json').write_text(json.dumps(doc))

    def edit(self, path, change):
        doc = json.loads(path.read_text())
        change(doc)
        path.write_text(json.dumps(doc))

    def assert_diagnostics(self, report, warnings):
        before = deepcopy(report)
        text = markdown(report, include_evidence=False)
        self.assertIn(f"Report errors: **{len(report['errors'])}**. Run warnings: **{len(warnings)}**.", text)
        rows = ['| ' + cell(error) + ' |' for error in report['errors']]
        if rows:
            self.assertIn('\n'.join(rows), text)
        warning_rows = ['| ' + ' | '.join(map(cell, row)) + ' |' for row in warnings]
        if warning_rows:
            self.assertIn('\n'.join(warning_rows), text)
        self.assertIn('use the JSON report', text)
        self.assertNotIn('## Complete evidence', text)
        self.assertNotIn('## Input diagnostics', markdown(report))
        self.assertEqual(report, before)
        return text

    def test_invalid_sources_caps_confidence_modes_and_saved_parity(self):
        self.add(self.b, 'mobile'); self.add(self.c, 'mobile', value=2500)
        self.add(self.b, 'known', device='desktop'); self.add(self.c, 'known', device='desktop')
        for cpu in (6, 8):
            self.add(self.b, f'profile-{cpu}', device='desktop', cpu=cpu)
        for cpu in (10, 12):
            self.add(self.c, f'profile-{cpu}', device='desktop', cpu=cpu)
        # Repeated warnings and control characters must survive as distinct source rows.
        warned = self.b / 'a|warn\nfile.json'
        (self.b / 'mobile-0.json').rename(warned)
        self.edit(warned, lambda d: d.update(runWarnings=['repeat|warning\r\nline'] * 2))
        self.edit(self.c / 'mobile-0.json', lambda d: d.update(runWarnings=['current warning']))
        self.edit(self.c / 'known-0.json', lambda d: d['audits'][AUDIT].update(
            scoreDisplayMode='error', errorMessage='bad|metric\nmessage'))
        saved = self.root / 'baseline.json'
        saved.write_text(json.dumps(create_baseline(self.b, self.p)))
        warnings = [('baseline', warned.name, 'repeat|warning\r\nline')] * 2 + [
            ('current', 'mobile-0.json', 'current warning')]
        for malformed in (False, True):
            if malformed:
                (self.c / 'broken.json').write_text('{')
            for mode in ({}, {'confidence_level': .95}, {'confidence_level': .95, 'confidence_gate': True}):
                for caps in ({}, {'mismatch_limit': 1, 'mismatch_field_limit': 1}):
                    with self.subTest(malformed=malformed, mode=mode, caps=caps):
                        direct = compare(self.b, self.c, self.p, **mode, **caps)
                        manifest = compare(saved, self.c, self.p, **mode, **caps)
                        self.assertEqual({k: v for k, v in manifest.items() if k != 'baseline_manifest'}, direct)
                        text = self.assert_diagnostics(direct, warnings)
                        self.assertEqual(text, markdown(manifest, include_evidence=False))
                        self.assertEqual(len(direct['errors']), 6 if malformed else 5)
                        self.assertIn(f'current/known-0.json: {AUDIT}: audit error: bad\\|metric message', text)
                        if malformed:
                            self.assertIn('current/broken.json: cannot read LHR:', text)
                            self.assertIsNone(next(r for r in direct['runs']['current'] if r['file']=='broken.json')['group_id'])
                        self.assertEqual((direct['decision'], direct['exit_code']), ('invalid', 2))
                        mobile = next(g for g in direct['groups'] if g['identity']['configSettings']['formFactor']=='mobile')
                        self.assertEqual(mobile['decision'], 'invalid' if malformed and mode.get('confidence_gate') else 'regression')
                        if mode:
                            self.assertEqual(mobile['metrics'][0]['confidence']['delta_interval'], None if malformed else [500, 500])
                            self.assertIn('## Confidence summaries', text)
                        if caps:
                            self.assertIn('total **4**, emitted **1**, omitted **3**', text)
                            self.assertIn('| Total changes | Emitted | Omitted |', text)
                        self.assertIn('## Group decisions', text)
                        self.assertIn('## Metric decisions', text)

    def test_warning_only_pass_and_explicit_zero_counts(self):
        self.add(self.b, 'run'); self.add(self.c, 'run')
        zero = self.assert_diagnostics(compare(self.b, self.c, self.p), [])
        self.assertNotIn('| Report error |', zero)
        self.assertNotIn('| Side | File | Warning |', zero)
        self.edit(self.b / 'run-0.json', lambda d: d.update(runWarnings=['one', 'one']))
        report = compare(self.b, self.c, self.p)
        self.assertEqual((report['decision'], report['exit_code']), ('pass', 0))
        self.assert_diagnostics(report, [('baseline', 'run-0.json', 'one')] * 2)

    def test_directory_and_metadata_errors_keep_report_order_without_recounting(self):
        self.add(self.c, 'run')
        self.edit(self.c / 'run-0.json', lambda d: d['configSettings'].pop('formFactor'))
        report = compare(self.b, self.c, self.p)
        self.assertTrue(report['errors'][0].startswith('baseline directory contains no immediate'))
        self.assertTrue(any('current/run-0.json: configSettings.formFactor' in e for e in report['errors']))
        # Renderer consumes the stored error list, including duplicates, rather than rebuilding it.
        report['errors'].extend(['same|error\nmessage'] * 2)
        text = self.assert_diagnostics(report, [])
        self.assertEqual(text.count('| same\\|error message |'), 2)

    def test_unvalidated_warning_payloads_are_preserved_without_character_or_key_iteration(self):
        self.add(self.b, 'run'); self.add(self.c, 'run')
        for payload in (None, 'raw|warning\ntext', {'z': [1, None], 'a': 'x|y'}, 7, [None, {'x': 1}, 'text']):
            with self.subTest(payload=payload):
                self.edit(self.b / 'run-0.json', lambda d: d.update(runWarnings=payload))
                report = compare(self.b, self.c, self.p)
                items = payload if isinstance(payload, list) else [payload]
                messages = [x if isinstance(x, str) else json.dumps(x, ensure_ascii=True, sort_keys=True, allow_nan=False)
                            for x in items]
                self.assert_diagnostics(report, [('baseline', 'run-0.json', x) for x in messages])
                self.assertEqual(report['runs']['baseline'][0]['runWarnings'], payload)
