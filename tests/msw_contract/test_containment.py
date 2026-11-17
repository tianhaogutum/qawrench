"""Process containment of real handler execution: encodings, cancellation, regex stalls, HEAD, statuses."""
import json
import os
import re
import threading
import time

import pytest

from qawrench.msw_contract import check, run_handler, utf8_prefix

from .support import CONTRACT, FIXTURES, HANDLERS, ROOT, UPSTREAM, requires_msw


def _example(scenario):
    return {"id": scenario, "method": "GET", "url": f"http://example.test/pets/1?scenario={scenario}", "headers": {}}


def test_byte_truncation_never_invents_replacement_characters():
    assert utf8_prefix("😀😀", 5) == "😀"
    assert utf8_prefix("😀", 3) == ""
    assert utf8_prefix("\ufeffabc", 10) == "abc"
    assert utf8_prefix("abc", 0) == ""


@requires_msw
def test_invalid_byte_streams_and_utf8_fail_while_bom_and_declared_text_encodings_decode():
    actual = {scenario: run_handler(HANDLERS, _example(scenario), cwd=ROOT)
              for scenario in ("invalid-utf8", "bad-stream", "bad-charset", "latin1", "bom")}
    assert actual["invalid-utf8"]["error"]["code"] == "body-encoding"
    assert actual["bad-stream"]["error"]["code"] == "body-stream"
    assert actual["bad-charset"]["error"]["code"] == "body-encoding"
    assert actual["latin1"]["response"]["body"] == "café"
    assert actual["latin1"]["response"]["bytes"] == 4
    assert json.loads(actual["bom"]["response"]["body"]) == {"id": 1, "name": "Milo"}


@requires_msw
def test_cancel_event_terminates_an_executing_worker_and_waits_for_its_exit():
    cancel = threading.Event()
    # Later than the original 0.8 s: the runner's own start-up (Node + msw import) comes first.
    timer = threading.Timer(2.0, cancel.set)
    timer.start()
    try:
        result = run_handler(HANDLERS, _example("abort"), cwd=ROOT, timeout_ms=5000, cancel=cancel)
    finally:
        timer.cancel()
    assert result["error"]["code"] == "cancelled"
    pid = int(re.search(r"WORKER_PID=(\d+)", result["diagnostics"]).group(1))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@requires_msw
def test_disabling_samples_removes_field_values_too_and_truncation_keeps_whole_characters():
    report = check(handlers_path=HANDLERS, schema_path=CONTRACT, cwd=ROOT, sample_bytes=0,
                   cases=[{"url": _example("stale")["url"]}])
    assert report["results"][0]["response"]["sample"] == ""
    assert "sample" not in report["results"][0]["issues"][0]
    unicode = check(handlers_path=HANDLERS, schema_path=CONTRACT, cwd=ROOT, sample_bytes=19,
                    cases=[{"url": _example("unicode")["url"]}])
    sample = unicode["results"][0]["response"]["sample"]
    assert len(sample.encode()) <= 19 and "\ufffd" not in sample


@requires_msw
def test_expensive_schema_regex_cannot_hang_the_check_and_the_next_case_restarts_and_passes():
    started = time.monotonic()
    report = check(handlers_path=HANDLERS, schema_path=FIXTURES / "slow-schema.json", cwd=ROOT, timeout_ms=1000,
                   cases=[{"url": _example("regex")["url"]}, {"url": _example("fast-regex")["url"]}])
    assert report["results"][0]["issues"][0]["code"] == "validation-timeout", report
    assert report["results"][1]["ok"] is True, report
    # The original bound was 4 s; Python validator start-up adds some headroom.
    assert time.monotonic() - started < 8


@requires_msw
def test_head_does_not_consume_a_never_ending_response_stream():
    actual = run_handler(FIXTURES / "relative.mjs", {"id": "head", "method": "HEAD",
                                                      "url": "https://example.test/pets/1", "headers": {}},
                         base_url="https://example.test/", cwd=ROOT, timeout_ms=1000)
    assert actual["response"]["bytes"] == 0, actual


@requires_msw
def test_msw_statuses_outside_the_http_range_cannot_pass_through_a_default_response():
    actual = run_handler(HANDLERS, _example("invalid-status"), cwd=ROOT)
    assert actual["error"]["code"] == "invalid-status"
    assert "777" in actual["error"]["message"]
    # This body otherwise satisfies OAI Petstore's default error schema.
    report = check(handlers_path=HANDLERS, schema_path=UPSTREAM / "petstore-3.0.yaml", cwd=ROOT,
                   cases=[{"url": _example("invalid-status")["url"]}])
    assert report["results"][0]["issues"][0]["code"] == "invalid-status"
