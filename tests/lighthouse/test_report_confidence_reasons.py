"""Concise confidence reasons are separate stored diagnostic occurrences."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from examples.lighthouse.fixture_factory import AUDIT, lhr, policy, write_runs
from qawrench.lighthouse import compare, create_baseline
from qawrench.lighthouse.report import markdown


def cell(value):
    return str(value).replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')


class ConciseConfidenceReasonTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.b, self.c = self.root/'b', self.root/'c'
        self.p = policy()
        self.p['metrics'][AUDIT]['relative_tolerance'] = 0

    def values(self, n=7, current=2000):
        write_runs(self.b, [2000]*n)
        write_runs(self.c, [current]*n)

    def edit(self, path, change):
        doc = json.loads(path.read_text())
        change(doc)
        path.write_text(json.dumps(doc))

    def assert_diagnostics(self, report):
        before = deepcopy(report)
        encoded = json.dumps(report, sort_keys=True)
        text = markdown(report, include_evidence=False)
        self.assertNotIn('## Confidence diagnostic reasons', markdown(report))
        if 'confidence' not in report:
            self.assertNotIn('## Confidence diagnostic reasons', text)
            return ''
        section = text.split('## Confidence diagnostic reasons\n', 1)[1].split('\n## ', 1)[0].split('\nComplete evidence is omitted', 1)[0]
        rows = ['| ' + ' | '.join(map(cell, (g['id'], m['audit'], reason))) + ' |'
                for g in report['groups'] for m in g['metrics'] for reason in m['confidence']['reasons']]
        self.assertIn(f'Confidence diagnostic reason occurrences: **{len(rows)}**.', section)
        self.assertEqual([line for line in section.splitlines() if line.startswith('| ')],
                         ['| Group | Audit | Reason |', '| --- | --- | --- |'] + rows if rows else [])
        # Active rows still come exclusively from metric.reasons, including in gate mode.
        active = text.split('## Metric decision reasons\n', 1)[1].split('\n## ', 1)[0]
        active_rows = ['| ' + ' | '.join(map(cell, (g['id'], m['audit'], reason))) + ' |'
                       for g in report['groups'] for m in g['metrics'] for reason in m['reasons']]
        self.assertEqual([line for line in active.splitlines() if line.startswith('| ')],
                         ['| Group | Audit | Reason |', '| --- | --- | --- |'] + active_rows if active_rows else [])
        self.assertTrue(text.startswith(markdown(report).split('\n## Complete evidence')[0] + '\n'))
        self.assertIn('## Confidence summaries', text)
        self.assertIn('## Input diagnostics', text)
        self.assertIn('use the JSON report', text)
        self.assertEqual(report, before)
        self.assertEqual(json.dumps(report, sort_keys=True), encoded)
        return section

    def test_five_sample_mad_pass_and_active_confidence_gate(self):
        self.values(5)
        for gate in (False, True):
            with self.subTest(gate=gate):
                report = compare(self.b, self.c, self.p, confidence_level=.95, confidence_gate=gate)
                metric = report['groups'][0]['metrics'][0]
                self.assertEqual((report['decision'], report['exit_code']), ('inconclusive', 3) if gate else ('pass', 0))
                self.assertEqual(metric['confidence']['status'], 'inconclusive')
                self.assertIsNone(metric['confidence']['delta_interval'])
                self.assertEqual(metric['confidence']['reasons'], ['at least 7 complete valid samples required on each side for this confidence level and policy'])
                section = self.assert_diagnostics(report)
                self.assertIn('7 complete valid samples', section)
                self.assertNotIn('adverse median delta', section)

    def test_missing_metrics_and_available_intervals_have_no_inferred_reasons(self):
        for n in (5, 8):
            self.values(n)
            self.edit(self.c/'run-001.json', lambda d: d['audits'][AUDIT].update(numericValue=None))
            for gate in (False, True):
                with self.subTest(n=n, gate=gate):
                    report = compare(self.b, self.c, self.p, confidence_level=.95, confidence_gate=gate)
                    metric = report['groups'][0]['metrics'][0]
                    self.assertEqual(metric['current']['count'], n-1)
                    self.assertEqual(metric['confidence']['reasons'],
                                     ['missing metric; confidence intervals never silently drop runs'] +
                                     (['at least 7 complete valid samples required on each side for this confidence level and policy'] if n==5 else []))
                    self.assert_diagnostics(report)
        # Complete available intervals: no diagnostic reason even for active overlap/regression.
        for values, decision in (([2000]*8, 'pass'), ([2500]*8, 'regression'), ([2000]*7+[2500], 'inconclusive')):
            write_runs(self.c, values)
            for gate in (False, True):
                with self.subTest(decision=decision, gate=gate):
                    report = compare(self.b, self.c, self.p, confidence_level=.95, confidence_gate=gate)
                    metric = report['groups'][0]['metrics'][0]
                    self.assertEqual(metric['confidence']['status'], 'available')
                    self.assertEqual(metric['confidence']['reasons'], [])
                    if gate:
                        self.assertEqual(metric['decision'], decision)
                    section = self.assert_diagnostics(report)
                    self.assertIn('occurrences: **0**.', section)
                    self.assertNotIn('| Group |', section)

    def test_order_duplicates_empty_escaping_zero_and_unrequested(self):
        self.values()
        report = compare(self.b, self.c, self.p, confidence_level=.95)
        group = report['groups'][0]
        group['id'] = 'z|group\r\nline'
        metric = group['metrics'][0]
        metric['audit'] = 'z|audit\nline'
        metric['confidence']['reasons'] = ['second|reason\r\nline', 'first', 'first', '']
        metric['mad_reasons'] = ['not-a-diagnostic']
        metric['relative_delta_reason'] = 'not-a-diagnostic'
        group['metrics'].append(dict(deepcopy(metric), audit='a-audit'))
        report['groups'].append(dict(deepcopy(group), id='a-group'))
        section = self.assert_diagnostics(report)
        self.assertIn('occurrences: **16**.', section)
        self.assertEqual(section.count('| z\\|group  line | z\\|audit line | first |'), 2)
        self.assertIn('| z\\|group  line | z\\|audit line | second\\|reason  line |', section)
        self.assertNotIn('not-a-diagnostic', section)
        for g in report['groups']:
            for m in g['metrics']:
                m['confidence']['status'] = 'invalid'
                m['confidence']['reasons'] = []
        self.assertIn('occurrences: **0**.', self.assert_diagnostics(report))
        del report['confidence']  # Stray per-metric data does not request confidence.
        self.assert_diagnostics(report)
        report = compare(self.b, self.c, self.p, confidence_level=.95)
        report['groups'] = []
        self.assertIn('occurrences: **0**.', self.assert_diagnostics(report))
        self.assert_diagnostics(compare(self.b, self.c, self.p))

    def test_known_unassigned_invalid_caps_and_manifest_parity(self):
        self.values()
        for directory in (self.b, self.c):
            for i in range(7):
                (directory/f'known-{i}.json').write_text(json.dumps(lhr(2000, 'https://synthetic.example.test/known')))
        for directory, cpus in ((self.b, (6, 8)), (self.c, (10, 12))):
            for cpu in cpus:
                doc = lhr(2000)
                doc['configSettings']['formFactor'] = 'desktop'
                doc['configSettings']['throttling']['cpuSlowdownMultiplier'] = cpu
                doc['userAgent'] = f'Synthetic/{cpu}'
                for i in range(7):
                    (directory/f'profile-{cpu}-{i}.json').write_text(json.dumps(doc))
        saved = self.root/'baseline.json'
        saved.write_text(json.dumps(create_baseline(self.b, self.p)))
        self.edit(self.c/'known-0.json', lambda d: d['audits'][AUDIT].update(numericValue=True))
        for unassigned in (False, True):
            if unassigned:
                (self.c/'broken.json').write_text('{')
            for gate in (False, True):
                with self.subTest(unassigned=unassigned, gate=gate):
                    options = dict(confidence_level=.95, confidence_gate=gate, mismatch_limit=1, mismatch_field_limit=1)
                    report = compare(self.b, self.c, self.p, **options)
                    self.assertEqual((report['decision'], report['exit_code']), ('invalid', 2))
                    for group in report['groups']:
                        metric = group['metrics'][0]
                        valid = not unassigned and group['identity']['finalUrl'].endswith('/') and group['identity']['configSettings']['formFactor']=='mobile'
                        self.assertEqual(metric['confidence']['status'], 'available' if valid else 'invalid')
                        self.assertEqual(metric['confidence']['reasons'], [] if valid else ['invalid gate input/statistics; confidence intervals suppressed'])
                    self.assert_diagnostics(report)
                    text = markdown(report, include_evidence=False)
                    self.assertEqual(text, markdown(compare(saved, self.c, self.p, **options), include_evidence=False))
                    self.assertIn('total **4**, emitted **1**, omitted **3**', text)
                    self.assertIn('| 2 | 1 | 1 | 1 | true |', text)
