# qawrench documentation

One page per command. Every page starts with what the command is for, then a quick
start whose flags match `qawrench <command> --help`.

| Group | Command | Page | Extra |
| --- | --- | --- | --- |
| Check | `check` | [check.md](check.md) | |
| Tests & flakiness | `flaky` | [flaky.md](flaky.md) | |
| | `har` | [har.md](har.md) | |
| | `trace-budget` | [trace-budget.md](trace-budget.md) | |
| | `statewatch` | [statewatch.md](statewatch.md) | `[statewatch]` |
| | `msw-contract` | [msw-contract.md](msw-contract.md) | `[openapi]` + Node.js 24+ |
| Accessibility & performance | `a11y` | [a11y.md](a11y.md) | |
| | `lighthouse` | [lighthouse.md](lighthouse.md) | |
| | `mermaid-paths` | [mermaid-paths.md](mermaid-paths.md) ([简体中文](mermaid-paths.zh-CN.md)) | |
| Pages & links | `page-diff` | [page-diff.md](page-diff.md) | `[html]` |
| | `structured-data` | [structured-data.md](structured-data.md) | `[structured]` |
| | `links` | [links.md](links.md), [FORMAT](links/FORMAT.md) | |
| Crawling | `frontier` | [frontier.md](frontier.md), [experiment](frontier/experiment.md), [upstream support](frontier/upstream-support.md) | `[frontier]` |
| | `crawlee-export` | [crawlee-export.md](crawlee-export.md) | `[parquet]` |

Attribution and licenses for every command: [THIRD_PARTY.md](../THIRD_PARTY.md).
