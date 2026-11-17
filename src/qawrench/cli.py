"""Top-level ``qawrench`` command: dispatches to one tool per subcommand.

Each tool lives in its own subpackage exposing ``cli.main(argv) -> int``. Tools
are imported lazily, so ``qawrench --help`` stays fast and a broken or missing
tool only affects its own command. Tools with heavy third-party dependencies
declare them in ``requires``; when one is not installed the command prints a
one-line install hint for the matching extra instead of a traceback.
"""
from __future__ import annotations

from dataclasses import dataclass
import difflib
import importlib
import importlib.util
import sys

from . import __version__

PROG = "qawrench"
REPO_URL = "https://github.com/tianhaogutum/qawrench"


@dataclass(frozen=True)
class Command:
    name: str
    # Dotted path of the module exposing ``main(argv)``.
    module: str
    summary: str
    group: str
    # Top-level import names of optional dependencies, and the extra that installs them.
    requires: tuple[str, ...] = ()
    extra: str | None = None


GROUPS = ("Check", "Tests & flakiness", "Accessibility & performance", "Pages & links", "Crawling")

COMMANDS: tuple[Command, ...] = (
    Command("check", "qawrench.check.cli",
            "Scan a folder of CI test artifacts and report what makes the run untrustworthy", "Check"),
    Command("flaky", "qawrench.flaky.cli",
            "Keep a local SQLite retry history for Playwright JSON and CTRF reports; find flaky tests",
            "Tests & flakiness"),
    Command("har", "qawrench.har.cli",
            "Select HAR fixtures, preflight request matches and plan collision-free replay rewrites",
            "Tests & flakiness"),
    Command("trace-budget", "qawrench.trace_budget.cli",
            "Inventory Playwright trace ZIPs against size budgets, compare them, repack losslessly",
            "Tests & flakiness"),
    Command("statewatch", "qawrench.statewatch.cli",
            "Trace env, cwd and module state that leaks between pytest tests; replay victim/writer pairs",
            "Tests & flakiness"),
    Command("msw-contract", "qawrench.msw_contract.cli",
            "Run real MSW handlers on explicit requests and check their responses against OpenAPI",
            "Tests & flakiness", requires=("yaml", "jsonschema", "referencing"), extra="openapi"),
    Command("a11y", "qawrench.a11y.cli",
            "Baseline axe and Pa11y results so CI fails only on new accessibility findings (SARIF)",
            "Accessibility & performance"),
    Command("lighthouse", "qawrench.lighthouse.cli",
            "Gate Lighthouse numeric audits on repeated runs with an explicit noise policy",
            "Accessibility & performance"),
    Command("mermaid-paths", "qawrench.mermaid_paths.cli",
            "Turn Mermaid flowcharts into author-reviewed, keyboard-followable static HTML path appendices",
            "Accessibility & performance"),
    Command("page-diff", "qawrench.page_diff.cli",
            "Compare two saved HTML snapshots with explicit, auditable noise rules", "Pages & links",
            requires=("bs4", "soupsieve"), extra="html"),
    Command("structured-data", "qawrench.structured_data.cli",
            "Compare JSON-LD and OpenGraph metadata between two saved HTML captures", "Pages & links",
            requires=("extruct", "lxml"), extra="structured"),
    Command("links", "qawrench.links.cli",
            "Convert Lychee link-check JSON to SARIF with source-verified locations and baselines",
            "Pages & links"),
    Command("frontier", "qawrench.frontier.cli",
            "Audit crawl request deduplication under an explicit URL policy and offline robots rules",
            "Crawling", requires=("w3lib",), extra="frontier"),
    Command("crawlee-export", "qawrench.crawlee_export.cli",
            "Export a local Crawlee dataset to typed, verified Parquet with resumable checkpoints",
            "Crawling", requires=("pyarrow",), extra="parquet"),
)
BY_NAME = {command.name: command for command in COMMANDS}

# Former standalone tool names, used only to point users at the new subcommand.
LEGACY = {
    "har-replay-doctor": "har", "retry-history-ledger": "flaky", "a11y-baseline-bridge": "a11y",
    "crawlee-parquet-export": "crawlee-export", "structured-data-parity": "structured-data",
    "trace-size-budget": "trace-budget", "lighthouse-noise-gate": "lighthouse",
    "stable-page-diff": "page-diff", "crawl-frontier-audit": "frontier",
    "verify-robots-witnesses": "frontier verify-witnesses", "lychee-sarif-map": "links",
    "pytest-statewatch": "statewatch", "msw-contract-check": "msw-contract",
}


def _present(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _installed(command: Command) -> bool:
    return _present(command.module.rpartition(".")[0])


def missing_dependency_message(command: Command, dependency: str) -> str:
    extra = f"{PROG}[{command.extra}]"
    return (f"{PROG} {command.name} needs {dependency}: pipx install '{extra}' "
            f"(or pip install '{extra}')\n")


def _missing_requirement(command: Command, exc: ModuleNotFoundError) -> str | None:
    """The optional dependency behind ``exc``, if it is one this command declares."""
    root = (exc.name or "").partition(".")[0]
    return root if root and root in command.requires else None


def overview() -> str:
    width = max(len(command.name) for command in COMMANDS) + 2
    lines = [
        f"{PROG} {__version__} - offline checks that make browser and E2E CI trustworthy",
        "",
        f"usage: {PROG} <command> [args...]",
        f"       {PROG} <command> --help",
    ]
    for group in GROUPS:
        lines += ["", f"{group}:"]
        for command in COMMANDS:
            if command.group != group:
                continue
            note = "" if _installed(command) else "  (coming soon)"
            lines.append(f"  {command.name.ljust(width)}{command.summary}{note}")
    lines += [
        "",
        "options:",
        "  -h, --help     show this overview",
        "  -V, --version  show the version and exit",
        "",
        f"Optional extras: {PROG}[parquet] (crawlee-export), {PROG}[structured] (structured-data),",
        f"{PROG}[html] (page-diff), {PROG}[frontier] (frontier), {PROG}[openapi] (msw-contract),",
        f"{PROG}[statewatch] (statewatch pytest plugin), or {PROG}[all].",
        "",
        f"Docs and issues: {REPO_URL}",
    ]
    return "\n".join(lines) + "\n"


def _unknown(name: str) -> int:
    message = f"{PROG}: unknown command {name!r}"
    if name in LEGACY:
        message += f"; {name} is now '{PROG} {LEGACY[name]}'"
    else:
        match = difflib.get_close_matches(name, [*BY_NAME, *LEGACY], n=1, cutoff=0.6)
        if match:
            suggestion = LEGACY.get(match[0], match[0])
            message += f"; did you mean '{suggestion}'?"
    sys.stderr.write(message + f"\nRun '{PROG} --help' to list commands.\n")
    return 2


def run(command: Command, argv: list[str]) -> int:
    if not _installed(command):
        sys.stderr.write(f"{PROG}: command {command.name!r} is not available in "
                         f"{PROG} {__version__} yet; see {REPO_URL}\n")
        return 2
    try:
        module = importlib.import_module(command.module)
        result = module.main(argv)
    except ModuleNotFoundError as exc:
        dependency = _missing_requirement(command, exc)
        if dependency is None:
            raise
        sys.stderr.write(missing_dependency_message(command, dependency))
        return 2
    return 0 if result is None else result


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(overview())
        return 0
    first, rest = args[0], args[1:]
    if first in ("-V", "--version"):
        sys.stdout.write(f"{PROG} {__version__}\n")
        return 0
    if first == "help":
        if not rest:
            sys.stdout.write(overview())
            return 0
        first, rest = rest[0], ["--help"]
    if first.startswith("-"):
        sys.stderr.write(f"{PROG}: unknown option {first!r}\nRun '{PROG} --help' for usage.\n")
        return 2
    command = BY_NAME.get(first)
    if command is None:
        return _unknown(first)
    return run(command, rest)


if __name__ == "__main__":
    raise SystemExit(main())
