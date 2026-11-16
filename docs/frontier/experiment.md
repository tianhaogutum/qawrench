# Recorded synthetic experiment

Run with `python examples/frontier/experiment.py --output examples/frontier/experiment-results.json`
from a qawrench checkout (recorded on Linux, Python 3.12.13, w3lib 2.3.1). Seed 39; 100 entity groups;
2,300 synthetic requests. Complete samples, labels and policies are in
[experiment-results.json](../../examples/frontier/experiment-results.json).

| Key algorithm | Unique keys | False-merge groups | False-merge occurrence pairs | Median ms |
| --- | ---: | ---: | ---: | ---: |
| raw_url_set | 1,900 | 100 | 600 | 0.105 |
| conservative | 2,200 | 0 | 0 | 0.246 |
| tracking_only | 2,100 | 0 | 0 | 1.683 |
| proposed | 1,800 | 100 | 100 | 1.679 |
| proposed_with_w3lib_sort | 1,400 | 300 | 300 | 27.009 |

Each of five repetitions measures key generation and set construction on the
same validated requests. Timings exclude parsing, report construction and I/O.
They are observations from this machine, not reproducible exact durations or
statistically significant performance estimates. Counts/labels are seeded and
reproducible. No throughput improvement is claimed.

The raw URL set wrongly combines GET and distinct POST bodies (six mixed-label
pairs per submit group). The conservative baseline protects those distinctions,
retains query order and records no labeled false merges. Removing only the
explicit tracking key reduces 100 further exact keys in this invented dataset;
fragmented variants still remain different. Its zero observed false merges do
not establish safety on real applications.

The sample proposed policy also drops fragments. It removes 400 baseline keys,
including 100 unsafe client-route merges. Enabling w3lib sorting and IDNA then
removes 800 baseline keys in total, with 300 mixed-label groups: meaningful
fragments, order-sensitive repeated arguments, and bare-versus-assigned empty
flags. It also merges some labeled-equivalent encoding/IDNA variations.
Repeated-pair multiplicity and distinct methods/bodies remain protected by
request keys; sorting intentionally loses repeated-argument order.

The script asserts these known failure counts, explicit repeated-argument and
body distinctions, seeded ordering, and agreement between the actual CLI output
and measured policy keys. Every mixed-label group is listed with its labels.
It executes a real local fixture CLI path and checks both written files.

The sample proposed policy's median key-processing time is 6.82 times the
conservative baseline (582.1% more time). This is a CPU cost, not a gain;
queue size reduction is only useful when the removed requests are equivalent.

**All labels and domains are synthetic. None of these results demonstrates
real-site improvements, semantic equivalence, adoption or market demand.**
