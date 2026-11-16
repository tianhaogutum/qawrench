"""Concise relative-delta evidence is rendered from stored fields only."""

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


class ConciseRelativeDeltaTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.b, self.c = self.root/'b', self.root/'c'
        self.p = policy()
        self.p['metrics'][AUDIT]['relative_tolerance'] = 0

    def values(self, baseline, current):
        for directory in (self.b, self.c):
            for path in directory.glob('*.json'):
                path.unlink()
        write_runs(self.b, baseline)
        write_runs(self.c, current)

    def assert_deltas(self, report):
        before = deepcopy(report)
        text = markdown(report, include_evidence=False)
        self.assertIn('## Relative deltas\n', text)
        self.assertNotIn('## Relative deltas\n', markdown(report))
        section = text.split('## Relative deltas\n', 1)[1].split('\n## ', 1)[0].split('\nComplete evidence is omitted', 1)[0]
        rows = ['| ' + ' | '.join(map(cell, (g['id'], m['audit'],
                'null' if m['relative_delta'] is None else m['relative_delta'],
                'null' if m['relative_delta_reason'] is None else m['relative_delta_reason']))) + ' |'
                for g in report['groups'] for m in g['metrics']]
        self.assertIn(f'Relative delta rows: **{len(rows)}**.', section)
        self.assertEqual([line for line in section.splitlines() if line.startswith('| ')],
                         ['| Group | Audit | Relative delta (ratio) | Stored reason |',
                          '| --- | --- | --- | --- |'] + rows if rows else [])
        self.assertIn('use the JSON report', text)
        self.assertEqual(report, before)
        return section

    def test_real_ratios_zero_and_zero_baseline_in_all_confidence_modes(self):
        for options in ({}, {'confidence_level': .95}, {'confidence_level': .95, 'confidence_gate': True}):
            for b, c, ratio, decision in ((2000, 2200, .1, 'regression'),
                    (2000, 1800, -.1, 'pass'), (-2000, -1800, .1, 'regression'),
                    (2000, 2000, 0.0, 'pass'), (0, 0, None, 'pass'), (0, 200, None, 'regression')):
                with self.subTest(options=options, baseline=b, current=c):
                    self.values([b]*7, [c]*7)
                    report = compare(self.b, self.c, self.p, **options)
                    m = report['groups'][0]['metrics'][0]
                    self.assertEqual((m['relative_delta'], m['decision']), (ratio, decision))
                    reason = 'baseline median is zero; relative delta undefined; absolute tolerance applies' if b == 0 else None
                    self.assertEqual(m['relative_delta_reason'], reason)
                    section = self.assert_deltas(report)
                    self.assertIn(f"| {'null' if ratio is None else ratio} | {'null' if reason is None else reason} |", section)
                    self.assertNotIn('%', section)

    def test_missing_invalid_and_overflow_do_not_fabricate_explanations(self):
        for b, c, decision in (([2000]*7, [None]*7, 'inconclusive'),
                ([None]*7, [2000]*7, 'inconclusive'), ([2000]*7, [True]*7, 'invalid'),
                ([-1e308]*7, [1e308]*7, 'invalid'),
                ([1e-308]*7, [1e308]*7, 'invalid'),
                ([-1e308]*3+[1]+[1e308]*3, [2000]*7, 'invalid')):
            with self.subTest(b=b, c=c):
                self.values(b, c)
                report = compare(self.b, self.c, self.p)
                m = report['groups'][0]['metrics'][0]
                self.assertEqual(m['decision'], decision)
                self.assertIsNone(m['relative_delta'])
                self.assertIsNone(m['relative_delta_reason'])
                section = self.assert_deltas(report)
                self.assertIn('| null | null |', section)
                self.assertNotIn('zero;', section)
                self.assertNotIn('overflow in', section)
        self.values([-1e308]*3+[0]+[1e308]*3, [2000]*7)
        report = compare(self.b, self.c, self.p)
        self.assertEqual(report['decision'], 'invalid')
        self.assertIn('baseline median is zero', self.assert_deltas(report))
        self.values([0]*7, [None]+[200]*6)
        report = compare(self.b, self.c, self.p)
        self.assertIsNone(report['groups'][0]['metrics'][0]['relative_delta'])
        self.assertIn('baseline median is zero', self.assert_deltas(report))

    def test_stored_values_order_blanks_null_and_escaping_without_recomputation(self):
        self.values([0]*7, [200]*7)
        report = compare(self.b, self.c, self.p, confidence_level=.95, confidence_gate=True)
        group = report['groups'][0]
        group['id'] = 'z|group\r\nline'
        m = group['metrics'][0]
        m.update(audit='z|audit\nline', relative_delta=0, relative_delta_reason='stored|reason\r\nline')
        m['reasons'] = ['active-only']
        m['mad_reasons'] = ['mad-only']
        m['confidence']['reasons'] = ['diagnostic-only']
        # Deliberately inconsistent medians and stored values catch recomputation/fallbacks.
        for ratio, reason in ((None, ''), (None, None), (-.1234567890123456, 'unchanged')):
            group['metrics'].append(dict(deepcopy(m), audit='a-audit', relative_delta=ratio, relative_delta_reason=reason))
        report['groups'].append(dict(deepcopy(group), id='a-group'))
        section = self.assert_deltas(report)
        self.assertIn('Relative delta rows: **8**.', section)
        self.assertIn('| z\\|group  line | z\\|audit line | 0 | stored\\|reason  line |', section)
        self.assertIn('| a-audit | null |  |', section)
        self.assertIn('| a-audit | null | null |', section)
        self.assertIn('| -0.1234567890123456 | unchanged |', section)
        for other in ('active-only', 'mad-only', 'diagnostic-only', 'baseline median is zero'):
            self.assertNotIn(other, section)
        m['relative_delta'] = 'stored|ratio\r\nline'
        self.assertIn('| stored\\|ratio  line |', self.assert_deltas(report))
        for key in ('relative_delta', 'relative_delta_reason'):
            broken = deepcopy(report)
            del broken['groups'][0]['metrics'][0][key]
            with self.subTest(missing=key), self.assertRaises(KeyError):
                markdown(broken, include_evidence=False)

    def test_no_groups_and_saved_baseline_caps(self):
        self.values([0]*7, [200]*7)
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
        for options in ({}, {'confidence_level': .95}, {'confidence_level': .95, 'confidence_gate': True}):
            report = compare(self.b, self.c, self.p, mismatch_limit=1, mismatch_field_limit=1, **options)
            section = self.assert_deltas(report)
            self.assertIn('| null | null |', section)  # unmatched groups carry no explanation
            self.assertEqual(markdown(report, include_evidence=False),
                             markdown(compare(saved, self.c, self.p, mismatch_limit=1, mismatch_field_limit=1, **options), include_evidence=False))
            text = markdown(report, include_evidence=False)
            self.assertIn('total **4**, emitted **1**, omitted **3**', text)
            self.assertIn('| 2 | 1 | 1 | 1 | true |', text)
            report['groups'] = []
            section = self.assert_deltas(report)
            self.assertIn('Relative delta rows: **0**.', section)
            self.assertNotIn('| Group |', section)
