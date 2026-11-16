"""Verify the actual seeded measurement, its labels, and both error counters."""

from collections import Counter
from pathlib import Path
import unittest

from examples.lighthouse.experiment import run_experiment
from qawrench.lighthouse import load_json

ROOT = Path(__file__).resolve().parents[2]


class ExperimentTests(unittest.TestCase):
    def test_seeded_end_to_end_measurements_match_saved_evidence(self):
        actual = run_experiment()
        self.assertEqual(actual, load_json(ROOT / 'examples' / 'lighthouse' / 'experiment-results.json'))
        self.assertEqual(actual['scenario_count'], 180)
        self.assertEqual(actual['low_sample']['decision'], 'inconclusive')
        self.assertEqual(actual['low_sample']['metric']['baseline']['count'], 2)
        counts = {method: Counter() for method in actual['methods']}
        for scenario in actual['scenarios']:
            self.assertEqual(len(scenario['baseline_values']), 7)
            self.assertEqual(len(scenario['current_values']), 7)
            metric = scenario['spread_aware_metric']
            self.assertEqual(scenario['current_values'], [v['value'] for v in metric['current']['values']])
            for method, decision in scenario['decisions'].items():
                counts[method][decision] += 1
                if scenario['shifted']:
                    counts[method]['false_negative'] += decision == 'pass'
                    counts[method]['undetected_shift'] += decision != 'regression'
                    counts[method]['shifted_inconclusive'] += decision == 'inconclusive'
                else:
                    counts[method]['false_positive'] += decision == 'regression'
                    counts[method]['unchanged_inconclusive'] += decision == 'inconclusive'
        for method, count in counts.items():
            for field, value in count.items():
                self.assertEqual(actual['methods'][method][field], value)
            result = actual['methods'][method]
            self.assertEqual(result['undetected_shift'], result['false_negative'] + result['shifted_inconclusive'])
        self.assertEqual(actual['methods']['single_run']['false_positive'], 24)
        self.assertEqual(actual['methods']['spread_aware']['unchanged_inconclusive'], 56)
        self.assertGreater(actual['methods']['spread_aware']['undetected_shift'],
                           actual['methods']['single_run']['undetected_shift'])

    def test_identical_seed_reproduces_full_values_and_decisions(self):
        self.assertEqual(run_experiment(trials=2, seed=123), run_experiment(trials=2, seed=123))


if __name__ == '__main__':
    unittest.main()
