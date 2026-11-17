# qawrench statewatch

`qawrench statewatch` (formerly `pytest-statewatch`) explains state that survives a
pytest test's setup, call and teardown. Its pytest plugin watches the environment
variables, working directory and JSON-native module attributes you select, and
records structured diffs, the first test after which each change was observed, and
the state later failing tests inherited. Optional bounded replays then run a failing
test alone and after each observed writer, in fresh processes.

Use it when a test passes alone but fails in the full suite, or when you suspect a
test leaves configuration behind, and you want to see **which values** changed and
after which test. Finding the polluting test by bisection is already solved by
detect-test-pollution and pytest-leak-finder (see [Prior art](#prior-art)); this
command attaches field-level evidence to such a result. The `qawrench statewatch`
command reads the plugin's local reports without importing your test suite.

No data is uploaded, no service is started, and there are no background workers.

## Install

Requires Python 3.11+ and pytest 8.2 to 9.x. The plugin needs the `statewatch` extra
(pytest, DeepDiff, pytest-replay and filelock), installed **into the environment that
runs your tests** so that pytest can load it:

```sh
pip install 'qawrench[statewatch]'      # in the test environment (or: uv add --dev 'qawrench[statewatch]')
qawrench statewatch --version           # prints the bare qawrench version, e.g. 0.1.0
```

`pipx install 'qawrench[statewatch]'` installs the `qawrench statewatch` report
reader on its own; a pipx environment is not visible to your project's pytest, so
use pip or uv in the test environment for the plugin. Reading reports and the
`--from-lanes` / `--from-doctor` adapters use only the standard library.

Installing qawrench registers the plugin with pytest through the `pytest11` entry
point `statewatch`. It stays **inactive** until `--statewatch POLICY.json` is
passed: ordinary pytest runs are not observed, nothing is written, and DeepDiff,
filelock and pytest-replay are not even imported. If `--statewatch` is passed
without the extra, pytest stops with a one-line usage error, for example
`qawrench statewatch needs deepdiff: pipx install 'qawrench[statewatch]' (or pip install 'qawrench[statewatch]')`.
With `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, load it explicitly with `-p statewatch`;
`-p no:statewatch` disables it. Uninstall the standalone `pytest-statewatch` package
if it is still present: both register a `statewatch` plugin with the same options.

`python -m qawrench statewatch` is equivalent to `qawrench statewatch`.

## Quick start

The demo lives in [`examples/statewatch/`](../examples/statewatch/). Run it from the
root of a qawrench source checkout, in an environment with the extra installed:

```sh
python -m pytest examples/statewatch/test_service.py \
  --statewatch examples/statewatch/policy.json --statewatch-output .statewatch/demo \
  --statewatch-confirm
qawrench statewatch .statewatch/demo/report.json
qawrench check .statewatch/demo
```

The pytest run deliberately exits **1**: `test_polluter` leaves an environment
variable, the working directory and a shared client configuration changed, then
`test_victim` fails. The first test's `monkeypatch` change is restored by its
fixture and is not a net leak. Confirmation runs serially with a 30-second timeout
per subprocess and at most 12 subprocesses. The report summary looks like this
(paths shortened):

```text
3 protocols; 1 with net changes
Status: observed (pytest exit 1)
cwd: first observed writer examples/statewatch/test_service.py::test_polluter
env:SW_SERVICE_MODE: first observed writer examples/statewatch/test_service.py::test_polluter
var:test_service:SETTINGS: first observed writer examples/statewatch/test_service.py::test_polluter
examples/statewatch/test_service.py::test_victim: pair_reproduced
  examples/statewatch/test_service.py::test_polluter -> examples/statewatch/test_service.py::test_victim: pair_reproduced
  cd /work/project && /work/project/.venv/bin/python -m pytest -p replay -p statewatch --replay=... ...
Details: /work/project/.statewatch/demo/events.jsonl
```

[`examples/statewatch/report.json`](../examples/statewatch/report.json) is a
**synthetic** report of this shape (made-up paths and run id), so you can try the
reader and `qawrench check` without running pytest:

```sh
qawrench statewatch examples/statewatch/report.json
qawrench check examples/statewatch
```

### pytest options

| Option | Default | Meaning |
| --- | --- | --- |
| `--statewatch POLICY.json` | (off) | Observe the state the policy selects; without it the plugin does nothing |
| `--statewatch-output DIR` | `.statewatch` | Output directory (locked for the run) |
| `--statewatch-fail-on-leak` | off | Turn an otherwise successful exit into 1 on an unallowed net change, a change between tests or an observer error |
| `--statewatch-confirm` | off | Replay each failing victim alone, then after each observed writer |
| `--statewatch-victim NODEID` | all | Restrict confirmation to this exact victim (repeatable) |
| `--statewatch-max-replays N` | 12 | Maximum number of replay subprocesses |
| `--statewatch-replay-timeout SECONDS` | 30 | Timeout per replay subprocess, in (0, 3600] |

### `qawrench statewatch` options

| Usage | Meaning |
| --- | --- |
| `qawrench statewatch [REPORT]` | Summarise a `report.json` (default `.statewatch/report.json`) |
| `qawrench statewatch --events EVENTS.jsonl [--limit N]` | List completed events, also after a crash (default limit 20) |
| `qawrench statewatch --from-lanes REPORT.json [--module PREFIX ...]` | Print a policy generated from a pytest-threadlanes report |
| `qawrench statewatch --from-doctor REPORT.json --policy POLICY.json` | Print a pytest command that traces values for a flakedoctor order repro |

Exit codes: `0` success, `2` invalid arguments or unreadable input (reported on
stderr as `qawrench statewatch: <message>`).

## Select state explicitly

```json
{
  "env": ["MY_APP_*", "TZ"],
  "cwd": true,
  "variables": ["my_app.settings:FLAGS", "logging:root.level"],
  "allow": ["env:MY_APP_EXPECTED_CACHE"],
  "redact": ["var:my_app.settings:FLAGS"],
  "max_value_bytes": 65536,
  "max_snapshot_bytes": 1048576
}
```

At least one of `env`, `cwd` or `variables` is required; unknown keys and duplicate
keys are rejected. `env` uses case-sensitive shell glob matching. `variables` uses
`module:attribute.path`, with no `eval`, calls, subscripts or implicit imports. The
module must already be loaded by the suite to inspect it; unloaded modules and
missing attributes are represented distinctly. Attribute lookup is static:
properties and dynamic `__getattr__` are not called. Values must be exact native
JSON types (dict with string keys, list, string, finite number, bool, null).
Snapshots are detached so in-place mutations are detected. Unsupported, cyclic,
oversize or unreadable values produce explicit incomplete-observation errors.
Declared variables that never have a readable value also make observation
incomplete, so a misspelled module or attribute cannot silently appear clean. The
total serialized-value budget (`max_snapshot_bytes`, 1 KiB to 8 MiB) bounds each
snapshot separately from the per-value limit (`max_value_bytes`, 128 B to 1 MiB).
Nested numeric/boolean type changes are preserved; dictionary key order is
immaterial. Only JSON value state is compared; object identity and aliasing are
not tracked. Unicode Python module and attribute identifiers are supported.

`allow` accepts full field globs (`env:NAME`, `cwd`, `var:module:attribute`) for
intentional changes. Allowed changes remain in the event trace but do not count as
leaks or replay candidates. Environment names containing TOKEN, SECRET, PASSWORD or
KEY are always redacted; `redact` extends that list. Redaction preserves change
detection while omitting values and DeepDiff payloads. Only selected state is
written; the full environment snapshot used to start replay subprocesses stays in
memory.

## Output

The output directory contains:

* `run.json`: versions (`statewatch_version` holds the qawrench version), the
  original pytest arguments, cwd and policy.
* `events.jsonl`: one flushed record per completed test protocol, with phase
  changes, net changes, inherited provenance and scoped-fixture context. Readable
  up to the last complete line after a crash.
* `report.json`: compact final test index, first observed writers, observer errors
  and optional confirmation results (`schema_version` 1). `qawrench check` reads it
  (see [check.md](check.md)).
* `replays/`: real pytest-replay JSONL input, child logs, child reports and
  shell-quoted reproduction commands when confirmation is enabled. Each run keeps a
  copy of its effective policy, so edits to the input policy during a test do not
  change a later replay's selection.

An output directory is exclusively locked for the run. Starting a new run
overwrites its manifest and event stream and removes the previous final report, so
an abrupt exit cannot leave a stale summary looking current. Replay artifacts are
grouped by run ID. After a crash:

```sh
qawrench statewatch --events .statewatch/events.jsonl --limit 100
```

The event reader accepts complete records before a truncated final line and
rejects malformed interior records. Redaction covers the observer's structured
state payloads; ordinary pytest output and tracebacks in child logs can contain
whatever the tests print. Child runtime is bounded; log file size is not capped.

`--statewatch-fail-on-leak` keeps ordinary test failures and pytest's other exit
statuses intact. `--statewatch-victim`, `--statewatch-max-replays` and
`--statewatch-replay-timeout` bound confirmation. A skipped, missing or incomplete
victim is never treated as a clean pass; an already-failing baseline is reported
without claiming an offender.

The plugin composes with pytest-rerunfailures: an entire retry protocol has one net
transition, while each attempt's phase changes remain in its event. pytest-xdist is
rejected before workers start. Exact duplicate node IDs (for example with
`--keep-duplicates`) cannot be represented faithfully by pytest-replay's
deduplicated input; observation is kept and confirmation is explicitly
inconclusive. A reproduced pair stays recorded when the replay budget runs out.
Interrupting confirmation kills its child process group and saves the completed
observations with pytest exit status 2.

## Interpretation and limits

The writer is the **first test protocol after which a change was observed**, not
the source line assigning it. Fixture setup and cleanup can be responsible; scoped
fixtures intentionally retain state across tests and are recorded as context.
Phase diffs help distinguish setup, test-body and teardown changes. Temporary
changes restored before a snapshot are invisible. Collection-time changes,
child-process state, external databases or files and unselected globals are outside
the observation scope. The observer never repairs application state. Provenance is
tracked per selected environment key or declared variable; nested DeepDiff paths do
not each have a separate writer history.

An inherited field is a temporal association. `pair_reproduced` means the victim
passed once alone, then failed once after the writer; it does not establish that a
particular field caused it or exclude independent nondeterminism. Replays use the
original pre-test environment and cwd but share the filesystem and external
services. The plugin verifies the executed node IDs and order and compares the
victim's failure phase, exception type and innermost source location; a different
failure or a reordering plugin gives an inconclusive result instead of a claimed
reproduction. Generated or relocated source can make this comparison conservative.

Observation supports serial pytest only. Confirming an existing `--replay`
invocation is rejected to avoid conflicting order inputs. Existing selection and
configuration arguments are kept for child runs; if another plugin removes or
reorders a requested test, inspect the child result and log. A suite with no
observed changes is not certified free of pollution in unselected or external
state.

## Use with an existing offender finder

For broad discovery, use [pytest-threadlanes](https://github.com/Heknon/pytest-threadlanes)
(Python 3.12+) and turn its JSON report into a narrow policy:

```sh
python -m pytest tests --lanes-detect --lanes-detect-report=shared.json
qawrench statewatch --from-lanes shared.json --module my_app.settings > watch.json
```

The adapter includes `ok`/`set-once` findings too: a value safe to share between
threads can still break test order. Repeat `--module` for application modules; the
explicit prefix avoids guessing module/attribute boundaries or importing application
code. Global findings select their whole root variable, which must be JSON-native.
Generated policies redact all selected values; review the selection and the `redact`
list to show benign values. Unmapped plugin objects, signals and non-root loggers are
reported on stderr; a truncated discovery report produces an explicitly partial
policy. Input is limited to 16 MiB. The verified format is pytest-threadlanes 0.1.0.

For an existing [pytest-flakedoctor](https://github.com/Therealdk8890/pytest-flakedoctor)
v1 **observed order** diagnosis, keep its verified repro blob and add a value trace:

```sh
python -m pytest tests --doctor --doctor-json=doctor.json
qawrench statewatch --from-doctor doctor.json --policy watch.json
```

The last command prints an argv-safe, quoted pytest command (`-p statewatch -p
flakedoctor --doctor-repro=... --statewatch=...`); run it from the suite's original
directory. It reuses flakedoctor's reproduction and does not repeat its search.
Incomplete or other-axis reports are rejected.

When no selected field explains a failing test, use the broader search of
[detect-test-pollution](https://github.com/asottile/detect-test-pollution):

```sh
detect-test-pollution --tests tests --failing-test tests/test_api.py::test_victim
```

Then pass its reported writer and victim to pytest with your policy and
`--statewatch-confirm` to get field-level evidence.

## Prior art

The original tool surveyed existing projects first (November 2026) and deliberately
reuses them instead of rebuilding them:

- [pytest-leak-finder](https://github.com/mgaitan/pytest-leak-finder),
  [detect-test-pollution](https://github.com/asottile/detect-test-pollution) and
  [pytest-find-dependencies](https://github.com/mrbean-bremen/pytest-find-dependencies)
  already find the polluting test (bisection or order search); they do not show
  which environment, cwd or module values changed. pytest-leak-finder's
  `demo/test_demo.py` is used, unmodified, as a test fixture.
- [pytest-threadlanes](https://github.com/Heknon/pytest-threadlanes) discovers
  shared-state changes broadly; [pytest-flakedoctor](https://github.com/Therealdk8890/pytest-flakedoctor)
  diagnoses order dependence with repeated, verified runs. statewatch imports their
  reports (`--from-lanes`, `--from-doctor`) rather than reimplementing either.
- [pytest-replay](https://github.com/ESSS/pytest-replay) supplies the execution and
  order layer for confirmation; [DeepDiff](https://zepworks.com/deepdiff/current/)
  computes the structured diffs (with `zip_ordered_iterables=True`, so positional
  type changes such as `[1, True]` to `[1.0, 1]` are kept);
  [filelock](https://py-filelock.readthedocs.io/) protects the output directory;
  pytest's own hooks provide the test lifecycle.
- pytest-rerunfailures, pytest-randomly and pytest-xdist were checked for
  composition (retries are observed per protocol, seeded randomly runs compose,
  xdist is rejected); pytest-envvars and pytest-isolate solve related but
  different problems (mock completeness, process isolation).

The remaining gap this command fills is narrow: explicit, bounded JSON before/after
values for selected state, phase versus net changes, chronological writer
provenance, and a single bounded pair check that is explicitly weaker than
flakedoctor's verification.
