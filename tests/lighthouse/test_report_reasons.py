"""Concise reason rows use active metric reasons, preserving every occurrence."""

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


class ConciseReasonTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.b, self.c = self.root / 'b', self.root / 'c'
        self.p = policy()
        self.p['metrics'][AUDIT]['relative_tolerance'] = 0

    def values(self, baseline, current):
        for directory in (self.b, self.c):
            directory.mkdir(exist_ok=True)
            for path in directory.glob('*.json'):
                path.unlink()
        write_runs(self.b, baseline)
        write_runs(self.c, current)

    def edit(self, path, change):
        doc = json.loads(path.read_text())
        change(doc)
        path.write_text(json.dumps(doc))

    def assert_reasons(self, report):
        before = deepcopy(report)
        encoded = json.dumps(report, sort_keys=True)
        rows = ['| ' + ' | '.join(map(cell, (g['id'], m['audit'], reason))) + ' |'
                for g in report['groups'] for m in g['metrics'] for reason in m['reasons']]
        text = markdown(report, include_evidence=False)
        section = text.split('## Metric decision reasons\n', 1)[1].split('\n## ', 1)[0].split('\nComplete evidence is omitted', 1)[0]
        self.assertIn(f'Metric reason occurrences: **{len(rows)}**.', section)
        actual = [line for line in section.splitlines() if line.startswith('| ')]
        self.assertEqual(actual, (['| Group | Audit | Reason |', '| --- | --- | --- |'] + rows) if rows else [])
        self.assertTrue(text.startswith(markdown(report).split('\n## Complete evidence')[0] + '\n'))
        self.assertIn('## Input diagnostics', text)
        self.assertIn('use the JSON report', text)
        self.assertNotIn('## Metric decision reasons', markdown(report))
        self.assertEqual(report, before)
        self.assertEqual(json.dumps(report, sort_keys=True), encoded)
        return section

    def test_missing_and_null_despite_enough_valid_samples(self):
        for kind in ('missing-audit', 'null-audit', 'missing-value', 'null-value'):
            self.values([2000]*8, [2000]*8)
            def change(doc):
                if kind == 'missing-audit':
                    del doc['audits'][AUDIT]
                elif kind == 'null-audit':
                    doc['audits'][AUDIT] = None
                elif kind == 'missing-value':
                    del doc['audits'][AUDIT]['numericValue']
                else:
                    doc['audits'][AUDIT]['numericValue'] = None
            self.edit(self.c / 'run-001.json', change)
            saved = self.root / 'baseline.json'
            saved.write_text(json.dumps(create_baseline(self.b, self.p)))
            for options in ({}, {'confidence_level': .95}, {'confidence_level': .95, 'confidence_gate': True}):
                with self.subTest(kind=kind, options=options):
                    report = compare(self.b, self.c, self.p, **options)
                    metric = report['groups'][0]['metrics'][0]
                    self.assertEqual((report['decision'], report['exit_code']), ('inconclusive', 3))
                    self.assertEqual(metric['current']['count'], 7)
                    expected = ('missing metric; confidence intervals never silently drop runs' if options.get('confidence_gate') else
                                'missing/null metric in one or more runs; runs are not silently dropped')
                    self.assertEqual(metric['reasons'], [expected])
                    section = self.assert_reasons(report)
                    self.assertIn(expected, section)
                    manifest = compare(saved, self.c, self.p, **options)
                    self.assertEqual(section, self.assert_reasons(manifest))

    def test_insufficient_noise_pass_regression_and_confidence_semantics(self):
        cases = [([2000]*2, [2500]*2, 'inconclusive', 'at least 5 valid samples'),
                 ([1800,1900,2000,2100,2200], [2000,2100,2200,2300,2400], 'inconclusive', 'noise band overlaps'),
                 ([2000]*5, [2000]*5, 'pass', 'adverse median delta + noise margin <= tolerance'),
                 ([2000]*7, [2500]*7, 'regression', 'adverse median delta > tolerance + noise margin')]
        for baseline, current, decision, reason in cases:
            self.values(baseline, current)
            for options in ({}, {'confidence_level': .95}, {'confidence_level': .95, 'confidence_gate': True}):
                with self.subTest(decision=decision, samples=len(baseline), options=options):
                    report = compare(self.b, self.c, self.p, **options)
                    metric = report['groups'][0]['metrics'][0]
                    section = self.assert_reasons(report)
                    if not options.get('confidence_gate'):
                        self.assertEqual(metric['decision'], decision)
                        self.assertIn(reason, section)
                        # Diagnostic confidence cannot silently replace MAD reasons.
                        self.assertNotIn('complete valid samples', section)
                    elif len(baseline) < 7:
                        self.assertEqual(metric['decision'], 'inconclusive')
                        self.assertEqual(metric['reasons'], ['at least 7 complete valid samples required on each side for this confidence level and policy'])
                        self.assertIn('7 complete valid samples', section)
                        self.assertNotIn(reason, section)
                        self.assertIn(reason, ' '.join(metric['mad_reasons']))
                    else:
                        self.assertEqual(metric['decision'], 'regression')
                        self.assertIn('adverse-change confidence interval lower endpoint > absolute tolerance', section)
                        self.assertNotIn(reason, section)
        self.values([2000]*7, [2000]*7)
        self.assertIn('upper endpoint <= absolute tolerance', self.assert_reasons(
            compare(self.b, self.c, self.p, confidence_level=.95, confidence_gate=True)))
        self.values([1900,1950,2000,2000,2000,2050,2100], [1950,2000,2050,2050,2050,2100,2150])
        report = compare(self.b, self.c, self.p, confidence_level=.95, confidence_gate=True)
        self.assertEqual(report['decision'], 'inconclusive')
        self.assertIn('confidence interval overlaps the absolute tolerance boundary', self.assert_reasons(report))

    def test_occurrence_order_duplicates_blank_messages_and_zero(self):
        self.values([0]*7, [0]*7)
        report = compare(self.b, self.c, self.p)
        self.assertIn('zero', report['groups'][0]['metrics'][0]['relative_delta_reason'])
        self.assertNotIn('relative delta undefined', self.assert_reasons(report))
        group = report['groups'][0]
        # Deliberately non-lexical stored order, including distinct audits and empty strings.
        group['id'] = 'z|group\r\nline'
        metric = group['metrics'][0]
        metric.update(audit='z|audit\nline', reasons=['second|message\r\nline', 'first', 'first', ''])
        group['metrics'].append(dict(deepcopy(metric), audit='a-audit', reasons=['last']))
        report['groups'].append(dict(deepcopy(group), id='a-group'))
        section = self.assert_reasons(report)
        self.assertEqual(section.count('| z\\|group  line | z\\|audit line | first |'), 2)
        self.assertIn('Metric reason occurrences: **10**.', section)
        for g in report['groups']:
            for m in g['metrics']:
                m['reasons'] = []
        self.assert_reasons(report)  # No per-metric placeholder rows.
        report['groups'][0]['metrics'] = []
        report['groups'] = []
        self.assert_reasons(report)

    def test_invalid_sources_combined_caps_and_manifest_parity(self):
        self.values([2000]*7, [2500]*7)
        for directory, cpus in ((self.b, (6, 8)), (self.c, (10, 12))):
            for cpu in cpus:
                doc = lhr(2000)
                doc['configSettings']['formFactor'] = 'desktop'
                doc['configSettings']['throttling']['cpuSlowdownMultiplier'] = cpu
                doc['userAgent'] = f'Synthetic/{cpu}'
                for i in range(7):
                    (directory / f'profile-{cpu}-{i}.json').write_text(json.dumps(doc))
        saved = self.root / 'baseline.json'
        saved.write_text(json.dumps(create_baseline(self.b, self.p)))
        self.edit(self.c/'run-001.json', lambda d: d['audits'][AUDIT].update(numericValue=True))
        for unassigned in (False, True):
            if unassigned:
                (self.c/'broken.json').write_text('{')
            for options in ({}, {'confidence_level': .95}, {'confidence_level': .95, 'confidence_gate': True}):
                with self.subTest(unassigned=unassigned, options=options):
                    caps = dict(mismatch_limit=1, mismatch_field_limit=1)
                    report = compare(self.b, self.c, self.p, **options, **caps)
                    manifest = compare(saved, self.c, self.p, **options, **caps)
                    self.assertEqual((report['decision'], report['exit_code']), ('invalid', 2))
                    text = markdown(report, include_evidence=False)
                    section = self.assert_reasons(report)
                    self.assertEqual(text, markdown(manifest, include_evidence=False))
                    self.assertIn('one or more runs have invalid input; see run errors', section)
                    self.assertIn('unmatched URL/version/config/browser group;', section)
                    self.assertIn('total **4**, emitted **1**, omitted **3**', text)
                    self.assertIn('| 2 | 1 | 1 | 1 | true |', text)
                    self.assertIn('Report errors:', text)
                    if options:
                        self.assertIn('## Confidence summaries', text)
                    self.assertNotIn('confidence intervals suppressed', section)
