"""Stored MAD operands remain visible and distinct from confidence decisions."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from examples.lighthouse.fixture_factory import AUDIT, lhr, policy, write_runs
from qawrench.lighthouse import compare, create_baseline
from qawrench.lighthouse.report import markdown


FIELDS = ('adverse_delta', 'tolerance', 'noise_margin', 'regression_boundary')
HEADER = ['| Group | Audit | Unit | Direction | Adverse delta | Tolerance | Noise margin | Regression boundary |',
          '| --- | --- | --- | --- | --- | --- | --- | --- |']
MODES = ({}, {'confidence_level': .95}, {'confidence_level': .95, 'confidence_gate': True})


def cell(value):
    return ('null' if value is None else str(value)).replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')


class ConciseMadOperandTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.b, self.c = self.root/'b', self.root/'c'
        self.p = policy()
        self.p['metrics'][AUDIT].update(absolute_tolerance=100, relative_tolerance=0)

    def values(self, baseline, current):
        for directory in (self.b, self.c):
            for path in directory.glob('*.json'):
                path.unlink()
        write_runs(self.b, baseline)
        write_runs(self.c, current)

    def assert_operands(self, report):
        before = deepcopy(report)
        text = markdown(report, include_evidence=False)
        self.assertIn('## MAD heuristic operands\n', text)
        self.assertNotIn('## MAD heuristic operands\n', markdown(report))
        section = text.split('## MAD heuristic operands\n', 1)[1].split('\n## ', 1)[0].split('\nComplete evidence is omitted', 1)[0]
        rows = ['| ' + ' | '.join(map(cell, (g['id'], m['audit'], str(m['unit']), str(m['direction']), *(m[k] for k in FIELDS)))) + ' |'
                for g in report['groups'] for m in g['metrics']]
        self.assertIn(f'MAD operand rows: **{len(rows)}**.', section)
        self.assertEqual([line for line in section.splitlines() if line.startswith('| ')],
                         HEADER + rows if rows else [])
        self.assertIn('not the confidence gate decision rule', section)
        self.assertIn('use the JSON report', text)
        self.assertEqual(report, before)
        return section

    def test_both_directions_and_equalities_in_every_confidence_mode(self):
        # Baseline median=2000/MAD=10; margin=20; tolerance=100; boundary=120.
        for direction, sign in (('lower', 1), ('higher', -1)):
            self.p['metrics'][AUDIT]['direction'] = direction
            for shift, expected in ((80, 'pass'), (100, 'inconclusive'),
                                    (120, 'inconclusive'), (121, 'regression'), (-100, 'pass')):
                for options in MODES:
                    with self.subTest(direction=direction, shift=shift, options=options):
                        self.values([1990]*3+[2000]+[2010]*3,
                                    [1990+sign*shift]*3+[2000+sign*shift]+[2010+sign*shift]*3)
                        report = compare(self.b, self.c, self.p, **options)
                        metric = report['groups'][0]['metrics'][0]
                        self.assertEqual(tuple(metric[k] for k in FIELDS), (shift, 100, 20.0, 120.0))
                        self.assertEqual(metric.get('mad_decision', metric['decision']), expected)
                        self.assertIn(f'| {float(shift)} | 100 | 20.0 | 120.0 |', self.assert_operands(report))
        # Active confidence disagrees with the stored heuristic; operands stay the same.
        self.p['metrics'][AUDIT]['direction'] = 'lower'
        self.values([2000]*7, [2050]*6+[2300])
        report = compare(self.b, self.c, self.p, **MODES[2])
        metric = report['groups'][0]['metrics'][0]
        self.assertEqual((metric['mad_decision'], metric['decision']), ('pass', 'inconclusive'))
        self.assertIn('| 50.0 | 100 | 0.0 | 100.0 |', self.assert_operands(report))
        # Mixed relative/absolute policy: use stored tolerance, even with diagnostic confidence.
        self.p['metrics'][AUDIT].update(absolute_tolerance=10, relative_tolerance=.1)
        report = compare(self.b, self.c, self.p, **MODES[1])
        self.assertIn('| 50.0 | 200.0 | 0.0 | 200.0 |', self.assert_operands(report))

    def test_missing_invalid_overflow_and_actual_zeros(self):
        for b, c, expected, decision in (
                ([0]*7, [0]*7, (0.0, 100, 0.0, 100.0), 'pass'),
                ([0]*7, [200]*7, (200.0, 100, 0.0, 100.0), 'regression'),
                ([2000]*7, [None]*7, (None,)*4, 'inconclusive'),
                ([None]*7, [2000]*7, (None,)*4, 'inconclusive'),
                ([2000]*7, [None]+[2200]*6, (200.0, 100, 0.0, 100.0), 'inconclusive'),
                ([2000]*7, [True]*7, (None,)*4, 'invalid'),
                ([2000]*7, [True]+[2200]*6, (200.0, 100, 0.0, 100.0), 'invalid'),
                ([-1e308]*7, [1e308]*7, (None,)*4, 'invalid'),
                ([1e-308]*7, [1e308]*7, (None,)*4, 'invalid'),
                ([-1e308]*3+[0]+[1e308]*3, [0]*7, (None,)*4, 'invalid')):
            for options in MODES:
                with self.subTest(b=b, c=c, options=options):
                    self.values(b, c)
                    report = compare(self.b, self.c, self.p, **options)
                    metric = report['groups'][0]['metrics'][0]
                    self.assertEqual(tuple(metric[k] for k in FIELDS), expected)
                    self.assertEqual(metric.get('mad_decision', metric['decision']), decision)
                    self.assert_operands(report)
        self.p['metrics'][AUDIT]['absolute_tolerance'] = 0
        self.values([0]*7, [0]*7)
        self.assertIn('| 0.0 | 0 | 0.0 | 0.0 |', self.assert_operands(compare(self.b, self.c, self.p)))

    def test_stored_values_precision_order_escaping_and_no_fallback(self):
        self.values([2000]*7, [2200]*7)
        report = compare(self.b, self.c, self.p, **MODES[2])
        group = report['groups'][0]
        group['id'] = 'z|group\r\nline'
        metric = group['metrics'][0]
        # Deliberately inconsistent with medians, direction and policy: never recompute.
        metric.update(audit='z|audit\nline', unit='stored|unit\r\nline',
                      direction='stored|direction\nline', adverse_delta=0, tolerance=None,
                      noise_margin=-0.0, regression_boundary=.1234567890123456)
        metric['reasons'] = ['active-only']
        metric['mad_reasons'] = ['mad-only']
        metric['confidence']['reasons'] = ['confidence-only']
        metric['relative_delta_reason'] = 'relative-only'
        group['metrics'].append(dict(deepcopy(metric), audit='a-audit', unit='', direction=None, adverse_delta=None,
                                     tolerance=0, noise_margin=None, regression_boundary=None))
        group['metrics'].append(deepcopy(metric))
        report['groups'].append(dict(deepcopy(group), id='a-group'))
        section = self.assert_operands(report)
        self.assertIn('MAD operand rows: **6**.', section)
        self.assertEqual(section.count('| z\\|group  line | z\\|audit line | stored\\|unit  line | stored\\|direction line | 0 | null | -0.0 | 0.1234567890123456 |'), 2)
        self.assertIn('| a-audit |  | None | null | 0 | null | null |', section)
        for other in ('active-only', 'mad-only', 'confidence-only', 'relative-only'):
            self.assertNotIn(other, section)
        # Escaping applies to every operand cell too, without new input validation.
        for key in FIELDS:
            metric[key] = 'stored|value\r\nline'
        self.assertIn('| stored\\|value  line | stored\\|value  line | stored\\|value  line | stored\\|value  line |', self.assert_operands(report))
        for key in (*FIELDS, 'unit', 'direction'):
            broken = deepcopy(report)
            del broken['groups'][0]['metrics'][0][key]
            with self.subTest(missing=key), self.assertRaises(KeyError):
                markdown(broken, include_evidence=False)
            # Complete renderer still serializes stored evidence without requiring these fields.
            self.assertIn('## Complete evidence', markdown(broken))

    def test_saved_caps_invalid_input_and_no_groups(self):
        self.values([2000]*7, [2200]*7)
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
        (self.c/'broken.json').write_text('{')
        for options in MODES:
            report = compare(self.b, self.c, self.p, mismatch_limit=1, mismatch_field_limit=1, **options)
            self.assertIn('| null | null | null | null |', self.assert_operands(report))
            text = markdown(report, include_evidence=False)
            self.assertEqual(text, markdown(compare(saved, self.c, self.p, mismatch_limit=1,
                             mismatch_field_limit=1, **options), include_evidence=False))
            self.assertIn('total **4**, emitted **1**, omitted **3**', text)
            self.assertIn('| 2 | 1 | 1 | 1 | true |', text)
            for heading in ('Input diagnostics', 'Metric decision reasons', 'Relative deltas'):
                self.assertIn('## '+heading, text)
            report['groups'] = []
            section = self.assert_operands(report)
            self.assertIn('MAD operand rows: **0**.', section)
            self.assertNotIn('| Group |', section)

    def test_multi_audit_units_directions_modes_and_saved_caps(self):
        self.values([2000]*7, [2050]*6+[2300])
        other = 'synthetic-throughput'
        self.p['metrics'][other] = dict(direction='higher', unit='unitless',
                                       absolute_tolerance=10, relative_tolerance=0)
        for directory, value in ((self.b, 100), (self.c, 80)):
            for path in directory.glob('*.json'):
                doc = json.loads(path.read_text())
                doc['audits'][other] = dict(id=other, scoreDisplayMode='numeric',
                                           numericUnit='unitless', numericValue=value)
                path.write_text(json.dumps(doc))
            # Two unmatched profiles per side exercise both mismatch caps with two metrics.
            for cpu in ((6, 8) if directory == self.b else (10, 12)):
                profile = deepcopy(doc)
                profile['configSettings']['formFactor'] = 'desktop'
                profile['configSettings']['throttling']['cpuSlowdownMultiplier'] = cpu
                profile['userAgent'] = f'Synthetic/{cpu}'
                for i in range(7):
                    (directory/f'profile-{cpu}-{i}.json').write_text(json.dumps(profile))
        saved = self.root/'baseline.json'
        saved.write_text(json.dumps(create_baseline(self.b, self.p)))
        for options in MODES:
            for caps in ({}, dict(mismatch_limit=1, mismatch_field_limit=1)):
                with self.subTest(options=options, caps=caps):
                    report = compare(self.b, self.c, self.p, **options, **caps)
                    matched = next(g for g in report['groups']
                                   if g['identity']['configSettings']['formFactor'] == 'mobile')
                    lower, higher = matched['metrics']
                    self.assertEqual((lower['unit'], lower['direction']), ('millisecond', 'lower'))
                    self.assertEqual((higher['unit'], higher['direction']), ('unitless', 'higher'))
                    self.assertEqual(tuple(lower[k] for k in FIELDS), (50, 100, 0, 100))
                    self.assertEqual(tuple(higher[k] for k in FIELDS), (20, 10, 0, 10))
                    self.assertEqual(higher['decision'], 'regression')
                    if options.get('confidence_gate'):
                        self.assertEqual((lower['mad_decision'], lower['decision']), ('pass', 'inconclusive'))
                    section = self.assert_operands(report)
                    self.assertIn('| millisecond | lower | 50.0 | 100 | 0.0 | 100.0 |', section)
                    self.assertIn('| unitless | higher | 20.0 | 10 | 0.0 | 10.0 |', section)
                    self.assertIn('MAD operand rows: **10**.', section)
                    self.assertEqual(markdown(report, include_evidence=False), markdown(
                        compare(saved, self.c, self.p, **options, **caps), include_evidence=False))
                    # Store context contrary to the audit ID, policy and numeric evidence.
                    lower.update(unit='unitless', direction='higher')
                    higher.update(unit='millisecond', direction='lower')
                    section = self.assert_operands(report)
                    self.assertIn('| unitless | higher | 50.0 | 100 | 0.0 | 100.0 |', section)
                    self.assertIn('| millisecond | lower | 20.0 | 10 | 0.0 | 10.0 |', section)
