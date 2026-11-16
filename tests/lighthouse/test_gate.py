import json
from fractions import Fraction
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from examples.lighthouse.fixture_factory import AUDIT, lhr, policy, write_runs
from qawrench.lighthouse import compare, load_json, validate_policy
from qawrench.lighthouse.core import median
from qawrench.lighthouse.report import markdown

ROOT = Path(__file__).resolve().parents[2]


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.b, self.c = self.root / 'b', self.root / 'c'
        self.p = policy()
        self.set_values([1980, 1990, 2000, 2010, 2020], [1990, 2000, 2010, 2020, 9000])

    def set_values(self, baseline, current):
        for directory in (self.b, self.c):
            directory.mkdir(exist_ok=True)
            for path in directory.glob('*.json'):
                path.unlink()
        write_runs(self.b, baseline)
        write_runs(self.c, current)

    def change(self, side, transform, index=1):
        path = side / f'run-{index:03d}.json'
        value = load_json(path)
        transform(value)
        path.write_text(json.dumps(value), encoding='utf-8')

    def evaluate(self):
        return compare(self.b, self.c, self.p)

    def metric(self, report=None):
        return (report or self.evaluate())['groups'][0]['metrics'][0]


class GateTests(FixtureCase):
    def test_known_median_mad_and_outlier(self):
        report = self.evaluate()
        metric = self.metric(report)
        self.assertEqual(report['decision'], 'pass')
        self.assertEqual(metric['baseline']['median'], 2000)
        self.assertEqual(metric['baseline']['mad'], 10)
        self.assertEqual(metric['current']['median'], 2010)
        self.assertEqual(metric['current']['mad'], 10)
        self.assertEqual(metric['delta'], 10)
        self.assertEqual(metric['relative_delta'], .005)
        self.assertEqual(metric['tolerance'], 100)
        self.assertEqual(metric['noise_margin'], 20)
        self.assertEqual([v['value'] for v in metric['current']['values']], [1990, 2000, 2010, 2020, 9000])

    def test_even_median_and_mad(self):
        self.set_values([1, 2, 4, 8, 9, 10], [1, 2, 4, 8, 9, 10])
        metric = self.metric()
        self.assertEqual(metric['baseline']['median'], 6)
        self.assertEqual(metric['baseline']['mad'], 3.5)
        self.assertEqual(median([1e308, 1e308]), 1e308)

    def test_consistent_regression(self):
        self.set_values([1980, 1990, 2000, 2010, 2020], [2480, 2490, 2500, 2510, 2520])
        result = self.evaluate()
        self.assertEqual((result['decision'], result['exit_code']), ('regression', 1))
        self.assertEqual(self.metric(result)['adverse_delta'], 500)

    def test_higher_better_and_improvement(self):
        self.p['metrics'][AUDIT]['direction'] = 'higher'
        self.set_values([2000]*5, [1500]*5)
        self.assertEqual(self.evaluate()['decision'], 'regression')
        self.set_values([2000]*5, [2500]*5)
        self.assertEqual(self.evaluate()['decision'], 'pass')

    def test_zero_baseline_never_divides_or_invents_percent(self):
        self.set_values([0]*5, [200]*5)
        metric = self.metric()
        self.assertEqual(metric['decision'], 'regression')
        self.assertIsNone(metric['relative_delta'])
        self.assertIn('zero', metric['relative_delta_reason'])
        self.assertEqual(metric['tolerance'], 100)
        self.set_values([0]*5, [0]*5)
        self.assertEqual(self.evaluate()['decision'], 'pass')

    def test_relative_tolerance_uses_absolute_baseline(self):
        self.set_values([-1000]*5, [-900]*5)
        rule = self.p['metrics'][AUDIT]
        rule.update(absolute_tolerance=0, relative_tolerance=.2)
        metric = self.metric()
        self.assertEqual(metric['tolerance'], 200)
        self.assertEqual(metric['relative_delta'], .1)
        self.assertEqual(metric['decision'], 'pass')

    def test_noise_band_and_boundary_equalities(self):
        self.set_values([1800, 1900, 2000, 2100, 2200], [2000, 2100, 2200, 2300, 2400])
        self.assertEqual(self.metric()['decision'], 'inconclusive')
        self.set_values([1800, 1900, 2000, 2100, 2200], [1800, 1900, 2000, 2100, 2200])
        self.assertEqual(self.metric()['decision'], 'inconclusive')  # high spread, zero median delta
        self.set_values([2000]*5, [2100]*5)
        self.assertEqual(self.metric()['decision'], 'pass')  # equality passes if margin is zero
        self.set_values([1980, 1990, 2000, 2010, 2020], [2100, 2110, 2120, 2130, 2140])
        self.assertEqual(self.metric()['decision'], 'inconclusive')  # exact upper boundary
        self.set_values([2000]*5, [2100.001]*5)
        self.assertEqual(self.metric()['decision'], 'regression')

    def test_noise_multiplier_zero_is_explicit_opt_out(self):
        self.p['noise_multiplier'] = 0
        self.set_values([1800, 1900, 2000, 2100, 2200], [2000, 2100, 2200, 2300, 2400])
        self.assertEqual(self.metric()['decision'], 'regression')

    def test_insufficient_samples(self):
        self.set_values([2000, 2010], [2500, 2510])
        report = self.evaluate()
        self.assertEqual((report['decision'], report['exit_code']), ('inconclusive', 3))
        self.assertIn('5 valid samples', ' '.join(self.metric(report)['reasons']))

    def test_missing_null_and_score_only_not_zero(self):
        for value in ('missing-audit', 'null-audit', 'null-value', 'missing-value'):
            with self.subTest(value=value):
                self.set_values([2000]*6, [2000]*6)
                def edit(doc):
                    if value == 'missing-audit':
                        del doc['audits'][AUDIT]
                    elif value == 'null-audit':
                        doc['audits'][AUDIT] = None
                    elif value == 'null-value':
                        doc['audits'][AUDIT]['numericValue'] = None
                    else:
                        del doc['audits'][AUDIT]['numericValue']
                self.change(self.c, edit)
                metric = self.metric()
                self.assertEqual(metric['current']['count'], 5)
                self.assertEqual(metric['current']['median'], 2000)
                self.assertEqual(metric['decision'], 'inconclusive')
                self.assertIsNone(metric['current']['values'][0]['value'])

    def test_bad_metric_types_and_units(self):
        for field, value in [('numericValue', True), ('numericValue', '2000'), ('numericValue', {}),
                             ('numericUnit', 'second'), ('id', 'other'), ('errorMessage', 'failed'),
                             ('scoreDisplayMode', 'error')]:
            with self.subTest(field=field, value=value):
                self.set_values([2000]*5, [2000]*5)
                self.change(self.c, lambda doc: doc['audits'][AUDIT].update({field: value}))
                self.assertEqual(self.evaluate()['decision'], 'invalid')

    def test_runtime_error_invalid_and_preserved(self):
        error = {'code': 'NO_FCP', 'message': 'Synthetic failure'}
        self.change(self.c, lambda doc: doc.update(runtimeError=error))
        report = self.evaluate()
        self.assertEqual(report['decision'], 'invalid')
        self.assertEqual(report['runs']['current'][0]['runtimeError'], error)
        self.assertEqual(report['runs']['current'][0]['metrics'][AUDIT]['value'], 1990)

    def test_mismatched_dimensions_are_not_pooled(self):
        changes = [lambda d: d.update(finalUrl='https://other.example.test/'),
                   lambda d: d.update(finalDisplayedUrl='https://synthetic.example.test/#after'),
                   lambda d: d['configSettings'].update(formFactor='desktop'),
                   lambda d: d['configSettings']['throttling'].update(cpuSlowdownMultiplier=8),
                   lambda d: d['configSettings']['screenEmulation'].update(width=500),
                   lambda d: d['configSettings'].update(extraUnrecognizedSetting=True),
                   lambda d: d['environment'].update(hostUserAgent='DifferentChrome/137')]
        for edit in changes:
            with self.subTest(edit=edit):
                self.set_values([2000]*5, [2000]*5)
                self.change(self.c, edit)
                report = self.evaluate()
                self.assertEqual(report['decision'], 'invalid')
                self.assertEqual(len(report['groups']), 2)
                self.assertTrue(any('unmatched' in e for e in report['errors']))

    def test_multiple_matched_groups_compare_independently(self):
        for side in (self.b, self.c):
            for i in range(5):
                value = 3000 if side == self.b else 3500
                (side / f'other-{i}.json').write_text(json.dumps(lhr(value, 'https://other.example.test/')))
        report = self.evaluate()
        self.assertEqual(len(report['groups']), 2)
        self.assertEqual(sorted(g['decision'] for g in report['groups']), ['pass', 'regression'])
        self.assertEqual(report['decision'], 'regression')

    def test_unsupported_versions_and_modes(self):
        for field, value in [('lighthouseVersion', '13.0.0'), ('lighthouseVersion', '12.8.1'),
                             ('gatherMode', 'timespan'), ('finalUrl', None)]:
            self.set_values([2000]*5, [2000]*5)
            self.change(self.c, lambda doc: doc.update({field: value}))
            self.assertEqual(self.evaluate()['decision'], 'invalid')

    def test_malformed_metadata(self):
        changes = [lambda d: d.pop('configSettings'), lambda d: d.update(configSettings=[]),
                   lambda d: d['configSettings'].pop('throttling'),
                   lambda d: d['configSettings']['throttling'].update(cpuSlowdownMultiplier=0),
                   lambda d: d['configSettings']['screenEmulation'].update(mobile='true'),
                   lambda d: d['configSettings']['screenEmulation'].pop('width'),
                   lambda d: d.update(environment=[]), lambda d: d.update(audits=[]),
                   lambda d: d.update(finalUrl='https://host:bad/'),
                   lambda d: d.update(finalUrl='relative/path')]
        for edit in changes:
            self.set_values([2000]*5, [2000]*5)
            self.change(self.c, edit)
            self.assertEqual(self.evaluate()['decision'], 'invalid')

    def test_invalid_json_and_unrepresentable_numbers(self):
        path = self.c / 'run-001.json'
        for raw in ('{', '[]', '{"a":1,"a":2}', '{"x": NaN}', '{"x": Infinity}',
                    '{"x": -Infinity}', '{"x": 1e999}', '{"x": ' + '9'*400 + '}'):
            with self.subTest(raw=raw[:40]):
                path.write_text(raw)
                report = self.evaluate()
                self.assertEqual(report['decision'], 'invalid')
                self.assertIn('run-001.json', ' '.join(report['errors']))
                json.dumps(report, allow_nan=False)
        path.write_bytes(b'\xff')
        self.assertEqual(self.evaluate()['decision'], 'invalid')

    def test_numeric_overflow_is_invalid_and_json_safe(self):
        self.set_values([-1e308]*5, [1e308]*5)
        report = self.evaluate()
        self.assertEqual(report['decision'], 'invalid')
        json.dumps(report, allow_nan=False)
        self.set_values([-1e308, -1e308, -1e308, 1e308, 1e308], [2000]*5)
        # Some deviations overflow, but raw MAD remains zero with a majority at the median.
        json.dumps(self.evaluate(), allow_nan=False)
        self.p['noise_multiplier'] = 1e308
        self.set_values([1980,1990,2000,2010,2020], [1990,2000,2010,2020,2030])
        report = self.evaluate()
        self.assertEqual(report['decision'], 'invalid')
        json.dumps(report, allow_nan=False)

    def test_empty_nonexistent_and_wrapped_inputs(self):
        for path in self.c.glob('*.json'):
            path.unlink()
        self.assertEqual(self.evaluate()['decision'], 'invalid')
        self.assertEqual(compare(self.b, self.root / 'absent', self.p)['decision'], 'invalid')
        (self.c / 'manifest.json').write_text('{"lhr": {}}')
        self.assertEqual(self.evaluate()['decision'], 'invalid')

    def test_all_files_are_accounted_for_and_non_json_is_not_executed(self):
        (self.c / 'unrelated.json').write_text('{}')
        marker = self.root / 'executed'
        (self.c / 'script.py').write_text(f"open({str(marker)!r}, 'w').write('bad')")
        report = self.evaluate()
        self.assertEqual(len(report['runs']['current']), 6)
        self.assertEqual(report['decision'], 'invalid')
        self.assertFalse(marker.exists())

    def test_second_metric_and_regression_precedence(self):
        self.p['metrics']['custom-throughput'] = {'direction': 'higher', 'unit': 'byte',
                                                'absolute_tolerance': 10, 'relative_tolerance': 0}
        for side in (self.b, self.c):
            for path in side.glob('*.json'):
                doc = load_json(path)
                doc['audits']['custom-throughput'] = {'id': 'custom-throughput', 'numericUnit': 'byte',
                                                      'numericValue': 1000 if side == self.b else 500}
                path.write_text(json.dumps(doc))
        self.change(self.c, lambda doc: doc['audits'][AUDIT].update(numericValue=None))
        self.assertEqual(self.evaluate()['decision'], 'regression')
        self.change(self.c, lambda doc: doc.update(runtimeError={'code': 'X'}))
        self.assertEqual(self.evaluate()['decision'], 'invalid')

    def test_reports_deterministic_complete_and_include_reasons(self):
        first, second = self.evaluate(), self.evaluate()
        self.assertEqual(first, second)
        text = markdown(first)
        self.assertIn('9000', text)
        self.assertIn('noise margin', text)
        self.assertIn('run-005.json', text)
        self.assertIn('configSettings', text)
        self.change(self.c, lambda doc: doc.update(runWarnings=['synthetic warning ```']))
        self.assertIn('````json', markdown(self.evaluate()))


class ConfidenceTests(FixtureCase):
    def confidence_report(self, level=.95):
        return compare(self.b, self.c, self.p, confidence_level=level)

    def test_exact_seven_run_change_interval_is_reproducible(self):
        self.set_values(list(range(1980, 2041, 10)), list(range(2180, 2241, 10)))
        report = self.confidence_report()
        confidence = self.metric(report)['confidence']
        self.assertEqual(confidence['status'], 'available')
        self.assertEqual(report['confidence']['min_samples_per_side'], 7)
        self.assertFalse(report['confidence']['affects_gate'])
        self.assertEqual(confidence['baseline']['interval'], [1980, 2040])
        self.assertEqual(confidence['current']['interval'], [2180, 2240])
        self.assertEqual(confidence['baseline']['order_statistic_ranks'], [1, 7])
        self.assertEqual(confidence['baseline']['coverage_lower_bound'], .984375)
        self.assertEqual(confidence['delta_interval'], [140, 260])
        self.assertEqual(confidence['adverse_delta_interval'], [140, 260])
        self.assertEqual(confidence['coverage_lower_bound'], .96875)
        self.assertEqual(report, self.confidence_report())
        # The method depends on the multiset, not run order or filenames.
        self.set_values(list(range(2040, 1979, -10)), list(range(2240, 2179, -10)))
        self.assertEqual(confidence, self.metric(self.confidence_report())['confidence'])
        self.assertIn('[140, 260]', markdown(report))

    def test_confidence_minimum_is_separate_from_mad_gate(self):
        original = self.evaluate()
        self.assertNotIn('confidence', original)
        report = self.confidence_report()
        self.assertEqual(report['decision'], original['decision'])
        self.assertEqual(report['exit_code'], 0)
        confidence = self.metric(report)['confidence']
        self.assertEqual(confidence['status'], 'inconclusive')
        self.assertIn('7', ' '.join(confidence['reasons']))
        for field in ('baseline', 'current', 'delta_interval', 'adverse_delta_interval', 'coverage_lower_bound'):
            self.assertIsNone(confidence[field])
        # Different confidence levels have different finite-interval minima.
        self.assertEqual(self.confidence_report(.90)['confidence']['min_samples_per_side'], 6)
        self.assertEqual(self.confidence_report(.99)['confidence']['min_samples_per_side'], 9)

    def test_narrowest_supported_ranks_even_samples_and_higher_better(self):
        self.set_values(list(range(1, 11)), list(range(11, 21)))
        self.p['metrics'][AUDIT]['direction'] = 'higher'
        confidence = self.metric(self.confidence_report())['confidence']
        self.assertEqual(confidence['baseline']['order_statistic_ranks'], [2, 9])
        self.assertEqual(confidence['baseline']['interval'], [2, 9])
        self.assertEqual(confidence['baseline']['coverage_lower_bound'], .978515625)
        self.assertEqual(confidence['delta_interval'], [3, 17])
        self.assertEqual(confidence['adverse_delta_interval'], [-17, -3])
        self.assertEqual(confidence['coverage_lower_bound'], .95703125)

    def test_ties_and_unequal_counts_are_supported(self):
        self.set_values([0]*7, [0]*10)
        confidence = self.metric(self.confidence_report())['confidence']
        self.assertEqual(confidence['delta_interval'], [0, 0])
        self.assertEqual(confidence['coverage_lower_bound'], .962890625)

    def test_confidence_honors_larger_configured_sample_minimum(self):
        self.set_values([2000]*7, [2200]*7)
        self.p['min_samples'] = 10
        report = self.confidence_report()
        self.assertEqual(report['confidence']['min_samples_per_side'], 10)
        self.assertEqual(report['confidence']['statistical_min_samples_per_side'], 7)
        self.assertEqual(self.metric(report)['confidence']['status'], 'inconclusive')

    def test_float_interval_endpoints_round_outward(self):
        self.set_values([-.1]*7, [.3]*7)
        interval = self.metric(self.confidence_report())['confidence']['delta_interval']
        exact = Fraction(.3) - Fraction(-.1)
        self.assertLessEqual(Fraction(interval[0]), exact)
        self.assertGreaterEqual(Fraction(interval[1]), exact)
        self.assertLess(interval[0], interval[1])

    def test_mixed_integer_float_endpoints_preserve_exact_difference(self):
        self.set_values([9007199254740992.0]*7, [9007199254740993]*7)
        confidence = self.metric(self.confidence_report())['confidence']
        self.assertEqual(confidence['delta_interval'], [1, 1])

    def test_missing_and_invalid_inputs_never_get_confidence_intervals(self):
        self.set_values([2000]*8, [2200]*8)
        self.change(self.c, lambda d: d['audits'][AUDIT].update(numericValue=None))
        report = self.confidence_report()
        confidence = self.metric(report)['confidence']
        self.assertEqual(confidence['status'], 'inconclusive')
        self.assertIsNone(confidence['delta_interval'])
        self.assertIn('missing', ' '.join(confidence['reasons']))
        (self.c / 'bad.json').write_text('{')
        report = self.confidence_report()
        self.assertEqual(report['decision'], 'invalid')
        self.assertEqual(self.metric(report)['confidence']['status'], 'invalid')
        self.assertIsNone(self.metric(report)['confidence']['delta_interval'])

    def test_confidence_overflow_is_explicit_and_json_safe(self):
        # Medians/MAD and the gate are finite, but extreme CI endpoints overflow.
        self.set_values([-1e308] + [0]*6, [0]*6 + [1e308])
        report = self.confidence_report()
        self.assertEqual(report['decision'], 'pass')
        confidence = self.metric(report)['confidence']
        self.assertEqual(confidence['status'], 'invalid')
        self.assertIn('overflow', ' '.join(confidence['reasons']))
        self.assertIsNone(confidence['delta_interval'])
        json.dumps(report, allow_nan=False)

    def test_bad_confidence_level_is_configuration_error(self):
        for level in (False, True, 0, 1, -1, 1.01, '0.95', float('nan'), float('inf')):
            with self.subTest(level=level), self.assertRaisesRegex(ValueError, 'confidence_level'):
                self.confidence_report(level)


class ConfigTests(unittest.TestCase):
    def test_invalid_policy_rejects_typos_and_implicit_defaults(self):
        mutations = [lambda p: p.update(unused=True), lambda p: p.pop('metrics'),
                     lambda p: p.update(schema_version=True), lambda p: p.update(min_samples=2),
                     lambda p: p.update(min_samples=True), lambda p: p.update(noise_multiplier=-1),
                     lambda p: p.update(noise_multiplier=float('nan')), lambda p: p.update(metrics={}),
                     lambda p: p['metrics'][AUDIT].update(direction='sideways'),
                     lambda p: p['metrics'][AUDIT].pop('absolute_tolerance'),
                     lambda p: p['metrics'][AUDIT].update(relative_tolerance=-1),
                     lambda p: p['metrics'][AUDIT].update(relative_tolerance=True),
                     lambda p: p['metrics'][AUDIT].update(unit='')]
        for mutation in mutations:
            p = policy()
            mutation(p)
            with self.subTest(policy=p), self.assertRaises(ValueError):
                validate_policy(p)
        with self.assertRaises(ValueError):
            validate_policy([])


class CliTests(FixtureCase):
    def cli(self, *extra):
        config = self.root / 'gate.json'
        config.write_text(json.dumps(self.p))
        return subprocess.run([sys.executable, '-m', 'qawrench.lighthouse', str(self.b), str(self.c),
                               '--config', str(config), *map(str, extra)], cwd=ROOT,
                              capture_output=True, text=True, timeout=15)

    def test_cli_report_and_dry_run(self):
        out, md = self.root / 'report.json', self.root / 'report.md'
        result = self.cli('--out', out, '--markdown', md)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(load_json(out)['decision'], 'pass')
        self.assertIn('9000', md.read_text())
        old = out.read_bytes()
        result = self.cli('--out', out, '--markdown', self.root / 'absent' / 'report.md', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(out.read_bytes(), old)
        self.assertEqual(json.loads(result.stdout)['decision'], 'pass')
        self.assertFalse((self.root / 'absent').exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX permission bits')
    def test_cli_reports_use_umask_not_private_temp_mode(self):
        out, md = self.root / 'mode.json', self.root / 'mode.md'
        previous = os.umask(0o022)
        try:
            result = self.cli('--out', out, '--markdown', md)
        finally:
            os.umask(previous)
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in (out, md):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644, path)
        self.assertEqual(list(self.root.glob('.noise-gate-*')), [])

    def test_cli_opt_in_confidence_json_markdown_and_bad_level(self):
        self.set_values(list(range(1980, 2041, 10)), list(range(2180, 2241, 10)))
        out, md = self.root / 'confidence.json', self.root / 'confidence.md'
        result = self.cli('--confidence-level', '.95', '--out', out, '--markdown', md)
        self.assertEqual(result.returncode, 1, result.stderr)
        report = load_json(out)
        self.assertEqual(self.metric(report)['confidence']['delta_interval'], [140, 260])
        self.assertIn('Confidence summaries', md.read_text())
        saved = out.read_bytes()
        result = self.cli('--confidence-level', '1', '--out', out)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)['decision'], 'invalid')
        self.assertEqual(out.read_bytes(), saved)

    def test_cli_exit_codes_and_json_reports(self):
        for baseline, current, code, decision in [([2000]*5, [2500]*5, 1, 'regression'),
                                                  ([2000]*2, [2500]*2, 3, 'inconclusive')]:
            self.set_values(baseline, current)
            result = self.cli()
            self.assertEqual(result.returncode, code, result.stderr)
            self.assertEqual(json.loads(result.stdout)['decision'], decision)
        (self.c / 'bad.json').write_text('{')
        result = self.cli()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)['decision'], 'invalid')

    def test_cli_protects_inputs_and_bad_output_paths(self):
        original = (self.b / 'run-001.json').read_bytes()
        for args in [('--out', self.b / 'run-001.json'), ('--out', self.c / 'new.json'),
                     ('--out', self.root / 'gate.json'), ('--out', self.root / 'missing' / 'r.json'),
                     ('--out', self.root / 'a', '--markdown', self.root / 'a')]:
            result = self.cli(*args)
            self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual((self.b / 'run-001.json').read_bytes(), original)
        link = self.root / 'symlink.json'
        link.symlink_to(self.b / 'run-001.json')
        self.assertEqual(self.cli('--out', link).returncode, 2)
        self.assertEqual((self.b / 'run-001.json').read_bytes(), original)

    def test_cli_invalid_config_and_usage(self):
        self.p['metrics'][AUDIT]['direction'] = 'unknown'
        result = self.cli()
        self.assertEqual(result.returncode, 2)
        self.assertIn('direction', result.stderr)
        result = subprocess.run([sys.executable, '-m', 'qawrench.lighthouse'], cwd=ROOT,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertIn('usage:', result.stderr)


if __name__ == '__main__':
    unittest.main()
