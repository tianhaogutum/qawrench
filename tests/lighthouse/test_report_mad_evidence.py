"""Concise active-confidence reports expose stored MAD evidence separately."""

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


class ConciseMadEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.b, self.c = self.root/'b', self.root/'c'
        self.p = policy()
        self.p['metrics'][AUDIT]['relative_tolerance'] = 0

    def values(self, current):
        for directory in (self.b, self.c):
            for path in directory.glob('*.json'):
                path.unlink()
        write_runs(self.b, [2000]*len(current))
        write_runs(self.c, current)

    def gated(self, **options):
        return compare(self.b, self.c, self.p, confidence_level=.95, confidence_gate=True, **options)

    def assert_evidence(self, report):
        before = deepcopy(report)
        text = markdown(report, include_evidence=False)
        self.assertNotIn('## Retained MAD evidence', markdown(report))
        if not report.get('confidence', {}).get('affects_gate'):
            self.assertNotIn('## Retained MAD evidence', text)
            return ''
        section = text.split('## Retained MAD evidence\n', 1)[1].split('\n## ', 1)[0].split('\nComplete evidence is omitted', 1)[0]
        decisions = ['| ' + ' | '.join(map(cell, (g['id'], m['audit'], m['mad_decision']))) + ' |'
                     for g in report['groups'] for m in g['metrics']]
        reasons = ['| ' + ' | '.join(map(cell, (g['id'], m['audit'], reason))) + ' |'
                   for g in report['groups'] for m in g['metrics'] for reason in m['mad_reasons']]
        self.assertIn(f'Retained MAD decision occurrences: **{len(decisions)}**. MAD reason occurrences: **{len(reasons)}**.', section)
        expected = (['| Group | Audit | MAD decision |', '| --- | --- | --- |'] + decisions if decisions else [])
        expected += (['| Group | Audit | Reason |', '| --- | --- | --- |'] + reasons if reasons else [])
        self.assertEqual([line for line in section.splitlines() if line.startswith('| ')], expected)
        self.assertTrue(text.startswith(markdown(report).split('\n## Complete evidence')[0] + '\n'))
        for heading in ('Metric decision reasons', 'Confidence diagnostic reasons', 'Input diagnostics'):
            self.assertIn('## ' + heading, text)
        self.assertIn('use the JSON report', text)
        self.assertEqual(report, before)
        return section

    def test_real_disagreements_agreements_and_insufficient_samples(self):
        for values, mad, active, interval in (
                ([2050]*6+[2300], 'pass', 'inconclusive', [50, 300]),
                ([2050]+[2200]*6, 'regression', 'inconclusive', [50, 200]),
                ([2100]*7, 'pass', 'pass', [100, 100]),
                ([2200]*7, 'regression', 'regression', [200, 200]),
                ([2000]*5, 'pass', 'inconclusive', None)):
            with self.subTest(mad=mad, active=active, interval=interval):
                self.values(values)
                report = self.gated()
                metric = report['groups'][0]['metrics'][0]
                self.assertEqual((metric['mad_decision'], metric['decision']), (mad, active))
                self.assertEqual(metric['confidence']['adverse_delta_interval'], interval)
                diagnostic = compare(self.b, self.c, self.p, confidence_level=.95)
                self.assertEqual(metric['mad_reasons'], diagnostic['groups'][0]['metrics'][0]['reasons'])
                section = self.assert_evidence(report)
                self.assertIn('| ' + mad + ' |', section)
                self.assertNotIn('adverse-change confidence interval', section)
                self.assertNotIn('complete valid samples', section)

    def test_stored_order_duplicates_blanks_escaping_and_explicit_zero(self):
        self.values([2000]*7)
        report = self.gated()
        group = report['groups'][0]
        group['id'] = 'z|group\r\nline'
        metric = group['metrics'][0]
        metric.update(audit='z|audit\nline', mad_decision='stored|decision\r\nline',
                      mad_reasons=['second|reason\r\nline', 'first', 'first', ''])
        metric['reasons'] = ['active-only']
        metric['confidence']['reasons'] = ['diagnostic-only']
        metric['relative_delta_reason'] = 'relative-only'
        group['metrics'].append(dict(deepcopy(metric), audit='a-audit', mad_reasons=[]))
        report['groups'].append(dict(deepcopy(group), id='a-group'))
        section = self.assert_evidence(report)
        self.assertIn('decision occurrences: **4**. MAD reason occurrences: **8**.', section)
        self.assertIn('| z\\|group  line | z\\|audit line | stored\\|decision  line |', section)
        self.assertEqual(section.count('| z\\|group  line | z\\|audit line | first |'), 2)
        for other in ('active-only', 'diagnostic-only', 'relative-only'):
            self.assertNotIn(other, section)
        for g in report['groups']:
            for m in g['metrics']:
                m['mad_reasons'] = []
        section = self.assert_evidence(report)
        self.assertIn('decision occurrences: **4**. MAD reason occurrences: **0**.', section)
        self.assertNotIn('| Group | Audit | Reason |', section)
        report['groups'] = []
        section = self.assert_evidence(report)
        self.assertIn('decision occurrences: **0**. MAD reason occurrences: **0**.', section)
        self.assertNotIn('| Group |', section)

    def test_inactive_absence_and_no_fallback_for_missing_stored_fields(self):
        self.values([2050]*6+[2300])
        for options in ({}, {'confidence_level': .95}):
            report = compare(self.b, self.c, self.p, **options)
            # Stray per-metric fields alone must never activate this section.
            report['groups'][0]['metrics'][0].update(mad_decision='stray', mad_reasons=['stray'])
            self.assert_evidence(report)
        for missing in ('mad_decision', 'mad_reasons'):
            report = self.gated()
            del report['groups'][0]['metrics'][0][missing]
            with self.subTest(missing=missing), self.assertRaises(KeyError):
                markdown(report, include_evidence=False)

    def test_invalid_input_caps_and_saved_baseline_parity(self):
        self.values([2050]*6+[2300])
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
        path = self.c/'run-001.json'
        doc = json.loads(path.read_text())
        doc['audits'][AUDIT]['numericValue'] = True
        path.write_text(json.dumps(doc))
        for unassigned in (False, True):
            if unassigned:
                (self.c/'broken.json').write_text('{')
            options = dict(confidence_level=.95, confidence_gate=True, mismatch_limit=1, mismatch_field_limit=1)
            report = compare(self.b, self.c, self.p, **options)
            self.assertEqual((report['decision'], report['exit_code']), ('invalid', 2))
            section = self.assert_evidence(report)
            self.assertIn('one or more runs have invalid input; see run errors', section)
            self.assertIn('unmatched URL/version/config/browser group;', section)
            self.assertNotIn('confidence intervals suppressed', section)
            self.assertEqual(markdown(report, include_evidence=False),
                             markdown(compare(saved, self.c, self.p, **options), include_evidence=False))
            text = markdown(report, include_evidence=False)
            self.assertIn('total **4**, emitted **1**, omitted **3**', text)
            self.assertIn('| 2 | 1 | 1 | 1 | true |', text)
