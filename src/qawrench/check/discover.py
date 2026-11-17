"""Find CI test artifacts by their content, not their names.

Only ``*.json``, ``*.har``, ``*.zip`` and ``*.xml`` candidates are opened, plus
Crawlee dataset directories (``datasets/<name>/000000001.json`` ...). A file
that is not a known artifact (``package.json``, a tsconfig, an HTML report's
assets) is skipped silently; a file whose name says it is a report but cannot
be parsed is reported. The reports of statewatch confirmation replays
(``replays/<run id>/<NNN>/report.json``) are summarised by their parent report
and skipped unless named explicitly. Nothing is written, nothing is extracted
to disk and no symlinked directory is followed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import zipfile
import zlib
from typing import Any

from .model import INFO, WARNING, Finding

# Directories that never hold artifacts worth checking. Dot-directories are
# skipped as well, except the ones CI tools write their output to.
IGNORE_DIRS = frozenset({"node_modules", "__pycache__", "venv", "site-packages"})
KEEP_DOT_DIRS = frozenset({".lighthouseci", ".statewatch"})
MAX_CANDIDATES = 50_000
MAX_JSON_BYTES = 64 * 1024 * 1024
SNIFF_BYTES = 4096
# Names that announce a report: such files are reported when they do not parse.
ANNOUNCED = re.compile(r"result|report|lighthouse|lhr|axe|pa11y|a11y|lychee|ctrf|junit|baseline", re.IGNORECASE)
DATASET_ITEM = re.compile(r"[0-9]{1,128}\.json")
DATASET_METADATA = "__metadata__.json"
JUNIT_ROOT = re.compile(r"<testsuites?[\s>/]")
# Copies of traces inside an HTML report are named by their SHA-1.
SHA1_NAME = re.compile(r"[0-9a-f]{40}\.zip")
# A statewatch confirmation child run: <output>/replays/<run id>/<NNN>/report.json.
STATEWATCH_RUN_ID = re.compile(r"[0-9a-f]{32}")
STATEWATCH_EXPERIMENT = re.compile(r"[0-9]{3,}")

KINDS = {
    "playwright-report": "Playwright JSON report",
    "ctrf": "CTRF report",
    "junit": "JUnit XML report",
    "har": "HAR file",
    "har-zip": "HAR archive",
    "trace": "Playwright trace",
    "axe": "axe-core results",
    "pa11y-ci": "Pa11y CI results",
    "a11y-baseline": "a11y baseline",
    "lhr": "Lighthouse report",
    "lychee": "lychee report",
    "statewatch-report": "pytest statewatch report",
    "parquet-export": "Parquet export manifest",
    "crawlee-dataset": "Crawlee dataset",
}
# Kinds whose parsed JSON is kept for the checks (the others re-read the file
# with their tool's own strict loader).
KEEP_DATA = frozenset({"axe", "pa11y-ci", "a11y-baseline", "lhr", "lychee", "statewatch-report"})


class DiscoveryError(ValueError):
    """A path given on the command line cannot be used (exit 2)."""


@dataclass
class Source:
    """One detected artifact. ``data`` is the parsed JSON for KEEP_DATA kinds."""

    path: Path
    display: str
    kind: str
    data: Any = None
    # Number of identical copies that were folded into this one (traces only).
    copies: int = 0

    @property
    def label(self) -> str:
        return KINDS[self.kind]


@dataclass
class Discovery:
    sources: list[Source] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    scanned: int = 0
    # Trace ZIP central-directory signature -> sources with that signature.
    _traces: dict = field(default_factory=dict, repr=False)


# --------------------------------------------------------------------------- classification

def _axe(value: Any) -> bool:
    engine = value.get("testEngine") if isinstance(value, dict) else None
    return (isinstance(engine, dict) and engine.get("name") == "axe-core"
            and isinstance(value.get("violations"), list))


def classify(data: Any, name: str = "") -> str | None:
    """Return the artifact kind of one parsed JSON document, or None."""
    if isinstance(data, list):
        return "axe" if data and all(_axe(item) for item in data) else None
    if not isinstance(data, dict):
        return None
    log = data.get("log")
    if isinstance(log, dict) and isinstance(log.get("entries"), list):
        return "har"
    if (isinstance(data.get("config"), dict) and isinstance(data.get("suites"), list)
            and isinstance(data.get("stats"), dict)):
        return "playwright-report"
    results = data.get("results")
    if isinstance(results, dict) and isinstance(results.get("tool"), dict) and isinstance(results.get("tests"), list):
        return "ctrf"
    if _axe(data):
        return "axe"
    if data.get("kind") == "a11y-baseline":
        return "a11y-baseline"
    if isinstance(results, dict) and all(key in data for key in ("total", "passes", "errors")):
        return "pa11y-ci"
    if isinstance(data.get("lighthouseVersion"), str) and isinstance(data.get("audits"), dict):
        return "lhr"
    if isinstance(data.get("error_map"), dict) and isinstance(data.get("success_map"), dict):
        return "lychee"
    if (data.get("schema_version") == 1 and isinstance(data.get("tests"), list)
            and isinstance(data.get("first_observed_writers"), dict) and isinstance(data.get("events_file"), str)):
        return "statewatch-report"
    if name.lower().endswith(".parquet.manifest.json") and "format_version" in data:
        return "parquet-export"
    return None


def candidate_type(path: Path) -> str | None:
    """How a file is opened: ``har-zip``, ``zip``, ``har``, ``json``, ``xml`` or None."""
    lower = path.name.lower()
    if lower.endswith(".har.zip"):
        return "har-zip"
    if lower.endswith(".zip"):
        return "zip"
    if lower.endswith(".har"):
        return "har"
    if lower.endswith(".json"):
        return "json"
    if lower.endswith(".xml"):
        return "xml"
    return None


def _trace_signature(path: Path):
    """Central-directory signature of a trace ZIP, or None if it is not one.

    Only the directory is read; no member is decompressed here.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError,
            EOFError, ValueError, zlib.error):
        return None
    if not any(info.filename.endswith(".trace") and not info.filename.startswith("resources/")
               for info in infos):
        return None
    return tuple(sorted((info.filename, info.CRC, info.file_size) for info in infos))


def _statewatch_replay(path: Path) -> bool:
    """True for the report of a statewatch confirmation child run."""
    parts = path.parts
    return (len(parts) >= 4 and parts[-4] == "replays" and bool(STATEWATCH_RUN_ID.fullmatch(parts[-3]))
            and bool(STATEWATCH_EXPERIMENT.fullmatch(parts[-2])))


def _junit(path: Path) -> bool:
    with path.open("rb") as stream:
        head = stream.read(SNIFF_BYTES).decode("utf-8", errors="ignore")
    # Skip the XML declaration and comments before the root element.
    head = re.sub(r"<\?.*?\?>|<!--.*?-->", "", head, flags=re.DOTALL)
    return bool(JUNIT_ROOT.match(head.lstrip("﻿ \t\r\n")))


# --------------------------------------------------------------------------- reading

def _read_json(path: Path) -> Any:
    with path.open("rb") as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError(f"larger than {MAX_JSON_BYTES // (1024 * 1024)} MiB; not inspected")
    return json.loads(raw.decode("utf-8-sig"))


def _parse_failure(display: str, rule: str, reason: str) -> Finding:
    return Finding("parse", rule, WARNING, f"cannot read this artifact: {reason}", display,
                   hint="the file may be truncated (an interrupted job or upload); it was not checked")


def _add_trace(path: Path, display: str, signature, result: Discovery) -> None:
    result._traces.setdefault(signature, []).append(Source(path, display, "trace"))


def _inspect(path: Path, display: str, how: str, explicit: bool, result: Discovery) -> None:
    result.scanned += 1
    if how == "har-zip":
        result.sources.append(Source(path, display, "har-zip"))
        return
    if how == "zip":
        signature = _trace_signature(path)
        if signature is not None:
            _add_trace(path, display, signature, result)
        elif explicit:
            result.findings.append(Finding("parse", "unrecognised", INFO,
                                           "not a Playwright trace (no .trace stream); skipped", display))
        return
    if how == "xml":
        try:
            junit = _junit(path)
        except OSError as exc:
            result.findings.append(_parse_failure(display, "unreadable", exc.strerror or type(exc).__name__))
            return
        if junit:
            result.sources.append(Source(path, display, "junit"))
        elif explicit:
            result.findings.append(Finding("parse", "unrecognised", INFO, "not a JUnit XML report; skipped", display))
        return
    announced = explicit or how == "har" or bool(ANNOUNCED.search(path.name))
    try:
        data = _read_json(path)
    except OSError as exc:
        result.findings.append(_parse_failure(display, "unreadable", exc.strerror or type(exc).__name__))
        return
    except UnicodeError:
        if announced:
            result.findings.append(_parse_failure(display, "invalid-encoding", "not UTF-8 text"))
        return
    except (ValueError, RecursionError) as exc:
        if announced:
            # json messages carry positions only, never file content.
            reason = (f"invalid JSON ({exc.msg} at line {exc.lineno})" if isinstance(exc, json.JSONDecodeError)
                      else str(exc) if isinstance(exc, ValueError) else "nesting too deep")
            result.findings.append(_parse_failure(display, "invalid-json", reason))
        return
    kind = classify(data, path.name)
    if how == "har" and kind is None and isinstance(data, dict):
        kind = "har"  # named *.har: let the har check explain what is wrong with it
    if kind == "statewatch-report" and not explicit and _statewatch_replay(path):
        return  # a confirmation replay: its parent report already summarises it
    if kind is not None:
        result.sources.append(Source(path, display, kind, data if kind in KEEP_DATA else None))
    elif explicit:
        result.findings.append(Finding(
            "parse", "unrecognised", INFO, "not a recognised test artifact (Playwright/CTRF report, HAR, "
            "trace, axe, Pa11y, Lighthouse, lychee or statewatch JSON); skipped", display))


def _crawlee_dataset(files: list[str], here: Path) -> bool:
    items = [name for name in files if name != DATASET_METADATA]
    return here.parent.name == "datasets" and bool(items) and all(DATASET_ITEM.fullmatch(n) for n in items)


def _walk(root: Path, raw: str, result: Discovery, seen: set) -> None:
    def fail(error: OSError):
        raise DiscoveryError(f"cannot read directory {error.filename}: {error.strerror}")

    for parent, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        here = Path(parent)
        dirs[:] = sorted(d for d in dirs if d not in IGNORE_DIRS
                         and (not d.startswith(".") or d in KEEP_DOT_DIRS)
                         and not (here / d).is_symlink())
        relative = here.relative_to(root)
        if _crawlee_dataset(files, here):
            result.scanned += 1
            result.sources.append(Source(here, os.path.normpath(os.path.join(raw, relative)), "crawlee-dataset"))
            continue
        for name in sorted(files):
            path = here / name
            how = candidate_type(path)
            if how is None or path.is_symlink() or not path.is_file():
                continue
            key = path.resolve()
            if key in seen:
                continue
            seen.add(key)
            if len(seen) > MAX_CANDIDATES:
                raise DiscoveryError(f"more than {MAX_CANDIDATES} candidate files; check a smaller folder")
            _inspect(path, os.path.normpath(os.path.join(raw, relative, name)), how, False, result)


def _fold_traces(result: Discovery) -> None:
    """Keep one source per identical trace; an HTML report copies every trace into data/."""
    for group in result._traces.values():
        group.sort(key=lambda source: (bool(SHA1_NAME.fullmatch(source.path.name)), source.display))
        keep = group[0]
        keep.copies = len(group) - 1
        result.sources.append(keep)
    result._traces.clear()


def discover(paths) -> Discovery:
    """Walk files and directories; raise DiscoveryError for unusable arguments."""
    result = Discovery()
    seen: set = set()
    for raw in paths:
        raw = str(raw)
        if not raw:
            raise DiscoveryError("empty path; use . for the current directory")
        path = Path(raw)
        if not path.exists():
            raise DiscoveryError(f"no such file or directory: {raw}")
        if path.is_dir():
            _walk(path, raw, result, seen)
            continue
        if not path.is_file():
            raise DiscoveryError(f"not a regular file or directory: {raw}")
        key = path.resolve()
        if key in seen:
            continue
        seen.add(key)
        how = candidate_type(path)
        if how is None:
            # An explicitly named file with an unusual name: sniff ZIP, XML or JSON.
            try:
                with path.open("rb") as stream:
                    head = stream.read(64).lstrip(b"\xef\xbb\xbf \t\r\n")
            except OSError as exc:
                raise DiscoveryError(f"cannot read {raw}: {exc.strerror}") from None
            how = "zip" if head[:2] == b"PK" else "xml" if head[:1] == b"<" else "json"
        _inspect(path, os.path.normpath(raw), how, True, result)
    _fold_traces(result)
    result.sources.sort(key=lambda source: source.display)
    return result
