# Demo CI artifacts for `qawrench check`

Everything in this folder is **synthetic**: made up for the README demo and the
tests, using `example.test` domains. No browser ran, no site was measured, no
request left this machine.

| Path | What it stands for |
| --- | --- |
| `test-results/results.json` | Playwright 1.56.1 JSON reporter output: 5 tests, 2 flaky, 1 failed on every retry |
| `test-results/checkout-applies-a-coupon-chromium-retry1/trace.zip` | A Playwright trace (version 8) whose last two screencast frames are missing |
| `recordings/api.har` | A HAR recording: one request recorded twice with different responses, a `cacheBust` query key |
| `a11y/axe-home.json` | axe-core 4.10.3 results for one page: 1 critical and 2 serious violations, no baseline |
| `lighthouse/lhr-*.json` | Three Lighthouse 12.8.2 runs of one URL; Total Blocking Time is noisy |
| `lychee.json` + `docs/` | lychee JSON output: a 404 and a timeout in two Markdown files |

```sh
cd examples/check
qawrench check
```

`tests/check/test_cli.py::test_demo_artifacts` pins the key lines of the output.
