# Synthetic fixtures and experiment

Every `baseline/*.json` and `current/*.json` file is an original **synthetic
Lighthouse 12.8.2 navigation LHR projection**. No page, browser, CI, or real
user was measured. These contain the fields this tool consumes; they are not
full LHRs for Lighthouse's HTML renderer. `fixture_factory.py` defines them.

The five baseline LCP values are 1980, 1990, 2000, 2010, 2020 ms. The five
current values are 1990, 2000, 2010, 2020, 9000 ms. Median/MAD moves from
2000/10 to 2010/10, and the policy in `gate.json` passes the comparison.
The 9000 ms outlier remains in both reports.

Run `python examples/lighthouse/experiment.py` from the repository root (or
`python experiment.py` from this directory). No server,
network, script hooks, or upstream binary is used. The experiment writes only
temporary synthetic input files, cleans them up, and prints its summary.
`--out PATH` explicitly saves all measurements; `experiment-results.json` is
that checked-in record, verified by the tests.

Seed 37037 generates 60 trials each of unchanged/noisy, large-shift, and
small-shift/noisy families, with seven LHRs per side. Gaussian draws use means
2000/2000, 2000/2450, 2000/2140 ms and standard deviations 180, 70, 180 ms
respectively. In the two noisy families each side independently has a 25%
chance of adding a 1200 ms outlier to one random run. Values are rounded to six
decimals and clamped to at least 1 ms. Labels reflect the generative change.

The single-run method compares the first generated baseline and current run,
using the same max(100 ms, 5% of baseline) tolerance. The spread-aware method
uses all seven runs, minimum five samples, and noise multiplier 2. A separate
two-run comparison returns inconclusive. This is not a reproduction of LHCI's
aggregation logic and cannot establish real-site improvements.
