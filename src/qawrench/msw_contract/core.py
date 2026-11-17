"""Run explicit request cases through real MSW handlers and audit the actual responses."""
from __future__ import annotations

import os
import threading
import time
import uuid

from ._js import js_json, utf8_bytes, utf8_length, utf8_prefix
from .errors import CheckCancelled, CheckError
from .inputs import MAX_CASES_BYTES, normalize_base_url, normalize_cases, read_data
from .runner import NodeRunner
from .session import ContractSession

_UNSET = object()
REPORT_VERSION = 1
REPORT_SCOPE = "explicit-examples-only"


def _require_dependencies() -> None:
    # Import the optional "openapi" extra up front so a missing package surfaces as
    # ModuleNotFoundError here (the dispatcher prints an install hint), not in a child process.
    import jsonschema  # noqa: F401
    import referencing  # noqa: F401
    import yaml  # noqa: F401


def _raise_if_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise CheckCancelled("Cancelled")


def check(*, handlers_path, schema_path, cases=_UNSET, cases_path=_UNSET, base_url: str | None = None,
          api_root: str = "/", timeout_ms: int = 3000, max_body_bytes: int = 1024 * 1024,
          sample_bytes: int = 2048, export_name: str = "handlers", cwd=None,
          cancel: threading.Event | None = None, node: str | None = None) -> dict:
    """Execute every explicit case with the real MSW handlers and check it against OpenAPI.

    Supply exactly one of ``cases`` (a list of case objects) or ``cases_path``.
    Returns the report dictionary (``version``, ``scope``, ``summary``, ``results``).
    Raises ``CheckError`` for invalid input or configuration, ``RuntimeUnavailable``
    when Node.js or the project's ``msw`` is missing, and ``CheckCancelled`` when
    ``cancel`` is set; running Node and validator processes are always stopped.
    """
    _require_dependencies()
    _raise_if_cancelled(cancel)
    if (cases is not _UNSET) == (cases_path is not _UNSET):
        raise CheckError("Provide exactly one of cases or cases_path")
    cwd = os.getcwd() if cwd is None else cwd
    for name, value in (("handlers_path", handlers_path), ("schema_path", schema_path), ("cwd", cwd),
                        ("export_name", export_name)):
        if isinstance(value, os.PathLike) and name != "export_name":
            value = os.fspath(value)
        if not isinstance(value, str) or not value:
            raise CheckError(f"{name} must be a non-empty string")
    if cases_path is not _UNSET:
        cases_path = os.fspath(cases_path) if isinstance(cases_path, os.PathLike) else cases_path
        if not isinstance(cases_path, str) or not cases_path:
            raise CheckError("cases_path must be a non-empty string")
    for name, value, low, high in (("timeout_ms", timeout_ms, 1, 120000),
                                   ("max_body_bytes", max_body_bytes, 1, 10 * 1024 * 1024),
                                   ("sample_bytes", sample_bytes, 0, 65536)):
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise CheckError(f"{name} must be an integer between {low} and {high}")
    if not isinstance(api_root, str) or not api_root.startswith("/") or "?" in api_root or "#" in api_root:
        raise CheckError("api_root must be a path beginning with /")
    cwd = os.path.abspath(os.fspath(cwd))
    handlers_path = os.path.abspath(os.path.join(cwd, os.fspath(handlers_path)))
    schema_path = os.path.abspath(os.path.join(cwd, os.fspath(schema_path)))
    try:
        os.stat(handlers_path)
    except OSError as exc:
        raise CheckError(f"{exc.strerror or exc}: {handlers_path}") from None
    base_url = normalize_base_url(base_url)
    data = cases if cases is not _UNSET else read_data(os.path.join(cwd, cases_path), MAX_CASES_BYTES)
    examples = normalize_cases(data, base_url)

    runner = NodeRunner(handlers_path, cwd=cwd, node=node)
    contract = ContractSession(schema_path, api_root, cwd)
    results = []
    try:
        runner.start(cancel)
        contract.start(cancel=cancel)
        for example in examples:
            _raise_if_cancelled(cancel)
            started = time.monotonic()
            actual = runner.run(example, export_name=export_name, timeout_ms=timeout_ms,
                                max_body_bytes=max_body_bytes, base_url=base_url, cancel=cancel)
            _raise_if_cancelled(cancel)
            remaining = timeout_ms - (time.monotonic() - started) * 1000
            if actual.get("error"):
                validation = {"issues": [{"path": "", **actual["error"]}]}
            elif remaining <= 0:
                validation = {"issues": [{"code": "validation-timeout", "path": "",
                                          "message": "Case deadline elapsed before response validation"}]}
            else:
                validation = contract.validate(example, actual["response"], remaining, cancel)
            result = {"id": example["id"], "request": {"method": example["method"], "url": example["url"]},
                      "ok": not validation["issues"], **validation}
            for issue in result["issues"]:
                if "sample" in issue:
                    if sample_bytes == 0:
                        issue.pop("sample", None)
                        issue.pop("sampleTruncated", None)
                    else:
                        limit = min(256, sample_bytes)
                        issue["sampleTruncated"] = bool(issue.get("sampleTruncated")) or \
                            utf8_length(issue["sample"]) > limit
                        issue["sample"] = utf8_prefix(issue["sample"], limit)
            response = actual.get("response")
            if response:
                result["response"] = {
                    "status": response["status"], "contentType": response["headers"].get("content-type"),
                    "bytes": response["bytes"], "sample": utf8_prefix(response["body"], sample_bytes),
                    "sampleTruncated": utf8_length(response["body"]) > sample_bytes,
                }
            if actual.get("diagnostics"):
                result["diagnostics"] = actual["diagnostics"]
            results.append(result)
    finally:
        contract.close()
        runner.close()
    failed = sum(1 for result in results if not result["ok"])
    return {"version": REPORT_VERSION, "scope": REPORT_SCOPE,
            "summary": {"total": len(results), "passed": len(results) - failed, "failed": failed},
            "results": results}


def format_text(report: dict) -> str:
    """The human-readable report (one line per case plus indented details)."""
    summary = report["summary"]
    lines = [f"{summary['passed']}/{summary['total']} examples passed; {summary['failed']} failed "
             "(explicit examples only)"]
    for result in report["results"]:
        response = result.get("response")
        suffix = ""
        if response:
            content_type = response["contentType"] if response["contentType"] is not None else "(no Content-Type)"
            suffix = f" -> {response['status']} {content_type}"
        lines.append(f"{'PASS' if result['ok'] else 'FAIL'} {result['id']}: {result['request']['method']} "
                     f"{result['request']['url']}{suffix}")
        for issue in result["issues"]:
            actual = "" if "sample" not in issue else f"; actual {issue['sample']}"
            lines.append(f"  {issue['code']} {issue['path'] or '/'}: {issue['message']}{actual}")
        if result.get("issuesTruncated"):
            lines.append(f"  showing {len(result['issues'])} of {result['issueCount']} schema errors")
        for warning in result.get("warnings") or []:
            lines.append(f"  warning: {warning['message']}")
        if not result["ok"] and response:
            truncated = " (truncated)" if response["sampleTruncated"] else ""
            lines.append(f"  response sample: {js_json(response['sample'])}{truncated}")
        if result.get("diagnostics"):
            lines.append(f"  handler output: {js_json(result['diagnostics'])}")
        if result.get("validationDiagnostics"):
            lines.append(f"  schema process output: {js_json(result['validationDiagnostics'])}")
    return "\n".join(lines) + "\n"


def write_report(path: str | os.PathLike[str], text: str) -> None:
    """Atomically replace ``path`` (mode 0600); a failed write keeps the previous report."""
    path = os.fspath(path)
    temporary = f"{path}.{os.getpid()}.{uuid.uuid4()}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(utf8_bytes(text))
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
