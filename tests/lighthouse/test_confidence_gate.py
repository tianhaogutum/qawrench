"""Opt-in absolute-tolerance gate contracts using synthetic offline LHRs."""

import json
import subprocess
import sys

from examples.lighthouse.fixture_factory import AUDIT, lhr
from qawrench.lighthouse import compare, create_baseline, load_json
from qawrench.lighthouse.report import markdown
from .test_gate import FixtureCase, ROOT


class ConfidenceGateTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.p['metrics'][AUDIT]['relative_tolerance'] = 0
        self.set_values([2000]*7, [2050]*6 + [2300])

    def gated(self, **kwargs):
        return compare(self.b, self.c, self.p, confidence_level=.95,
                       confidence_gate=True, **kwargs)

    def test_crossing_interval_abstains_from_both_mad_pass_and_regression(self):
        for values, mad, interval in (([2050]*6 + [2300], 'pass', [50, 300]),
                                      ([2050] + [2200]*6, 'regression', [50, 200])):
            with self.subTest(mad=mad):
                self.set_values([2000]*7, values)
                diagnostic = compare(self.b, self.c, self.p, confidence_level=.95)
                self.assertEqual(diagnostic['decision'], mad)
                report = self.gated()
                self.assertEqual((report['decision'], report['exit_code']), ('inconclusive', 3))
                metric = self.metric(report)
                self.assertEqual(metric['confidence']['adverse_delta_interval'], interval)
                self.assertEqual(metric['mad_decision'], mad)
                self.assertEqual(metric['mad_reasons'], self.metric(diagnostic)['reasons'])
                self.assertIn('tolerance', ' '.join(metric['reasons']))
                self.assertTrue(report['confidence']['affects_gate'])
                self.assertEqual(report['groups'][0]['decision'], 'inconclusive')
                self.assertIn('Confidence gate', markdown(report))
                self.assertNotIn('Diagnostic only', markdown(report))

    def test_exact_tolerance_endpoints_both_directions_and_improvements(self):
        cases = (([2100]*7, 'pass'), ([2050]*6 + [2100], 'pass'),
                 ([2100]*6 + [2200], 'inconclusive'), ([2101]*7, 'regression'),
                 ([1800]*7, 'pass'))
        for direction in ('lower', 'higher'):
            self.p['metrics'][AUDIT]['direction'] = direction
            for current, expected in cases:
                # Reflect around baseline to keep the same adverse intervals.
                if direction == 'higher':
                    current = [4000 - v for v in current]
                with self.subTest(direction=direction, expected=expected, current=current):
                    self.set_values([2000]*7, current)
                    self.assertEqual(self.gated()['decision'], expected)
        self.p['metrics'][AUDIT].update(direction='lower', absolute_tolerance=0)
        self.set_values([0]*7, [0]*7)
        self.assertEqual(self.gated()['decision'], 'pass')
        self.set_values([0]*7, [1]*7)
        self.assertEqual(self.gated()['decision'], 'regression')

    def test_confidence_mode_uses_interval_instead_of_mad_margin(self):
        self.set_values([2000]*7, [2000, 2010, 2020, 2030, 2040, 2050, 2100])
        self.p['noise_multiplier'] = 100
        self.assertEqual(self.evaluate()['decision'], 'inconclusive')
        self.assertEqual(self.gated()['decision'], 'pass')

    def test_insufficient_and_missing_samples_never_fall_back_to_mad(self):
        self.set_values([2000]*5, [2000]*5)
        report = self.gated()
        self.assertEqual((report['decision'], report['exit_code']), ('inconclusive', 3))
        self.assertEqual(self.metric(report)['mad_decision'], 'pass')
        self.assertIn('7 complete', ' '.join(self.metric(report)['reasons']))
        self.set_values([2000]*8, [2000]*8)
        self.change(self.c, lambda d: d['audits'][AUDIT].update(numericValue=None))
        report = self.gated()
        self.assertEqual(report['decision'], 'inconclusive')
        self.assertIsNone(self.metric(report)['confidence']['adverse_delta_interval'])
        self.assertIn('missing', ' '.join(self.metric(report)['reasons']))
        self.p['min_samples'] = 9
        self.set_values([2000]*8, [2000]*8)
        self.assertIn('9 complete', ' '.join(self.metric(self.gated())['reasons']))

    def test_invalid_input_and_interval_overflow_are_invalid(self):
        (self.c / 'bad.json').write_text('{')
        report = self.gated()
        self.assertEqual((report['decision'], report['exit_code']), ('invalid', 2))
        self.assertEqual(self.metric(report)['decision'], 'invalid')
        self.set_values([-1e308] + [0]*6, [0]*6 + [1e308])
        report = self.gated()
        self.assertEqual((report['decision'], report['exit_code']), ('invalid', 2))
        self.assertEqual(self.metric(report)['mad_decision'], 'pass')
        self.assertIn('overflow', ' '.join(self.metric(report)['reasons']))
        json.dumps(report, allow_nan=False)

    def test_bad_options_and_relative_or_mixed_policy_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'confidence_level'):
            compare(self.b, self.c, self.p, confidence_gate=True)
        for value in (None, 0, 1, 'true', [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'confidence_gate'):
                compare(self.b, self.c, self.p, confidence_level=.95, confidence_gate=value)
        for mixed in (False, True):
            if mixed:
                self.p['metrics']['other'] = dict(self.p['metrics'][AUDIT])
                self.p['metrics']['other']['relative_tolerance'] = .01
            else:
                self.p['metrics'][AUDIT]['relative_tolerance'] = .01
            with self.subTest(mixed=mixed), self.assertRaisesRegex(ValueError, 'relative_tolerance'):
                self.gated()
            self.p['metrics'][AUDIT]['relative_tolerance'] = 0

    def test_default_and_explicit_false_preserve_report_exactly(self):
        for level in (None, .95):
            old = compare(self.b, self.c, self.p, confidence_level=level)
            explicit = compare(self.b, self.c, self.p, confidence_level=level, confidence_gate=False)
            self.assertEqual(old, explicit)
            self.assertNotIn('mad_decision', self.metric(old))
            if level:
                self.assertFalse(old['confidence']['affects_gate'])
                self.assertIn('Diagnostic only', markdown(old))

    def test_saved_baseline_uses_same_gate(self):
        manifest = self.root / 'baseline.json'
        manifest.write_text(json.dumps(create_baseline(self.b, self.p)))
        report = compare(manifest, self.c, self.p, confidence_level=.95, confidence_gate=True)
        self.assertEqual(report['groups'], self.gated()['groups'])

    def test_metric_aggregation_preserves_regression_and_invalid_precedence(self):
        other = 'other-audit'
        self.p['metrics'][other] = dict(self.p['metrics'][AUDIT])
        for side, value in ((self.b, 2000), (self.c, 2200)):
            for path in side.glob('*.json'):
                doc = load_json(path)
                doc['audits'][other] = dict(doc['audits'][AUDIT], id=other, numericValue=value)
                path.write_text(json.dumps(doc))
        report = self.gated()
        self.assertEqual((report['decision'], report['exit_code']), ('regression', 1))
        self.assertEqual([m['decision'] for m in report['groups'][0]['metrics']],
                         ['inconclusive', 'regression'])
        self.change(self.c, lambda d: d['audits'][other].update(numericUnit='second'))
        report = self.gated()
        self.assertEqual((report['decision'], report['exit_code']), ('invalid', 2))

    def test_unmatched_profiles_leave_valid_group_gated_but_overall_invalid(self):
        for side, cpu in ((self.b, 4), (self.c, 8)):
            for index in range(7):
                doc = lhr(2000, 'https://synthetic.example.test/other')
                doc['configSettings']['throttling']['cpuSlowdownMultiplier'] = cpu
                (side / f'other-{index}.json').write_text(json.dumps(doc))
        report = self.gated()
        self.assertEqual((report['decision'], report['exit_code']), ('invalid', 2))
        valid = next(g for g in report['groups'] if not g['errors'])
        self.assertEqual(valid['decision'], 'inconclusive')
        self.assertEqual(valid['metrics'][0]['mad_decision'], 'pass')
        self.assertEqual(valid['metrics'][0]['confidence']['status'], 'available')
        for group in report['groups']:
            if group is not valid:
                self.assertEqual(group['metrics'][0]['decision'], 'invalid')

    def cli(self, *extra):
        config = self.root / 'policy.json'
        config.write_text(json.dumps(self.p))
        return subprocess.run([sys.executable, '-m', 'qawrench.lighthouse', str(self.b), str(self.c),
                               '--config', str(config), *map(str, extra)], cwd=ROOT,
                              capture_output=True, text=True, timeout=15)

    def test_cli_gate_outputs_and_negative_options_preserve_files(self):
        out, md = self.root / 'report.json', self.root / 'report.md'
        result = self.cli('--confidence-level', '.95', '--confidence-gate', '--out', out, '--markdown', md)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(load_json(out)['decision'], 'inconclusive')
        self.assertIn('Confidence gate', md.read_text())
        before = out.read_bytes(), md.read_bytes()
        for args in (('--confidence-gate',), ('--confidence-gate', '--confidence-level', 'nan')):
            result = self.cli(*args, '--out', out, '--markdown', md)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(json.loads(result.stderr)['decision'], 'invalid')
            self.assertEqual((out.read_bytes(), md.read_bytes()), before)
        self.p['metrics'][AUDIT]['relative_tolerance'] = .05
        result = self.cli('--confidence-level', '.95', '--confidence-gate', '--out', out, '--markdown', md)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('relative_tolerance', result.stderr)
        self.assertEqual((out.read_bytes(), md.read_bytes()), before)
