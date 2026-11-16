"""Enumerate equiprobable median-side patterns to verify exact coverage."""

from fractions import Fraction
from itertools import product
import unittest

from qawrench.lighthouse.confidence import _median_interval


class ExactCoverageTests(unittest.TestCase):
    def test_coverage_and_narrowest_ranks_against_all_sign_patterns(self):
        target = Fraction(39, 40)  # 97.5% per side for 95% joint coverage.
        for n in (7, 10):
            with self.subTest(n=n):
                interval, coverage = _median_interval(list(range(n)), target)
                rank = interval['order_statistic_ranks'][0]
                covered, narrower_covered = 0, 0
                for signs in product((-1, 1), repeat=n):
                    values = sorted(signs)
                    covered += values[rank - 1] <= 0 <= values[n - rank]
                    narrower_covered += values[rank] <= 0 <= values[n - rank - 1]
                self.assertEqual(Fraction(covered, 2**n), coverage)
                self.assertGreaterEqual(coverage, target)
                self.assertLess(Fraction(narrower_covered, 2**n), target)


if __name__ == '__main__':
    unittest.main()
