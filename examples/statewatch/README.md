# statewatch demo

A small, locally authored pytest example for `qawrench statewatch`. No service is
contacted: `example.invalid` is a reserved name.

| Path | What it is |
| --- | --- |
| `test_service.py` | Three tests sharing an HTTP client configuration: `test_monkeypatch_is_clean` restores its change, `test_polluter` leaves an environment variable, the working directory and `SETTINGS` changed, `test_victim` then fails |
| `policy.json` | The observation policy: `SW_SERVICE_*` variables, the working directory and `test_service:SETTINGS` |
| `report.json` | **Synthetic**: a hand-written `report.json` in the shape the plugin writes for this demo with `--statewatch-confirm` (made-up paths and run id), for trying the reader and `qawrench check` without running pytest |

Run from the repository root, with `qawrench[statewatch]` installed in the test
environment:

```sh
python -m pytest examples/statewatch/test_service.py \
  --statewatch examples/statewatch/policy.json --statewatch-output .statewatch/demo \
  --statewatch-confirm                  # exits 1: test_victim fails after test_polluter
qawrench statewatch .statewatch/demo/report.json
qawrench check examples/statewatch      # reads the synthetic report.json
```

See [docs/statewatch.md](../../docs/statewatch.md).
