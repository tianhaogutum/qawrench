"""Report output: atomic writes, the text format and the JSON shape."""
import json
import os

import pytest

from qawrench.msw_contract import format_text, write_report
from qawrench.msw_contract._js import dumps_report, js_json, js_number

REPORT = {
    "version": 1, "scope": "explicit-examples-only", "summary": {"total": 3, "passed": 2, "failed": 1},
    "results": [
        {"id": "list-pets", "request": {"method": "GET", "url": "http://petstore.swagger.io/v1/pets?limit=2"},
         "ok": True, "operation": "GET /pets", "statusKey": "200", "mediaType": "application/json",
         "bodyValidation": "schema", "issueCount": 0, "issuesTruncated": False, "issues": [],
         "response": {"status": 200, "contentType": "application/json", "bytes": 22,
                      "sample": '[{"id":1,"name":"Milo"}]', "sampleTruncated": False}},
        {"id": "stale-id", "request": {"method": "GET", "url": "http://petstore.swagger.io/v1/pets/1"},
         "ok": False, "operation": "GET /pets/{petId}", "statusKey": "200", "mediaType": "application/json",
         "bodyValidation": "schema", "issueCount": 1, "issuesTruncated": False,
         "issues": [{"code": "schema", "path": "/id", "message": "must be integer", "keyword": "type",
                     "schemaPath": "#/properties/id/type", "sample": '"1"', "sampleTruncated": False}],
         "response": {"status": 200, "contentType": "application/json", "bytes": 24,
                      "sample": '{"id":"1","name":"Milo"}', "sampleTruncated": False}},
        {"id": "missing-pet", "request": {"method": "GET", "url": "http://petstore.swagger.io/v1/pets/missing"},
         "ok": True, "operation": "GET /pets/{petId}", "statusKey": "default", "mediaType": "application/json",
         "bodyValidation": "schema", "issueCount": 0, "issuesTruncated": False, "issues": [],
         "response": {"status": 404, "contentType": "application/json", "bytes": 36,
                      "sample": '{"code":404,"message":"No such pet"}', "sampleTruncated": False}},
    ],
}


def test_text_report_matches_the_documented_example():
    assert format_text(REPORT) == (
        "2/3 examples passed; 1 failed (explicit examples only)\n"
        "PASS list-pets: GET http://petstore.swagger.io/v1/pets?limit=2 -> 200 application/json\n"
        "FAIL stale-id: GET http://petstore.swagger.io/v1/pets/1 -> 200 application/json\n"
        '  schema /id: must be integer; actual "1"\n'
        '  response sample: "{\\"id\\":\\"1\\",\\"name\\":\\"Milo\\"}"\n'
        "PASS missing-pet: GET http://petstore.swagger.io/v1/pets/missing -> 404 application/json\n"
    )


def test_text_report_lists_truncation_warnings_and_process_output():
    result = {"id": "x", "request": {"method": "GET", "url": "http://a.test/"}, "ok": False,
              "issueCount": 25, "issuesTruncated": True,
              "issues": [{"code": "timeout", "path": "", "message": "Handler process exceeded 3000 ms"}],
              "warnings": [{"code": "schema-warning", "message": 'unknown format "x" ignored'}],
              "diagnostics": "log\n", "validationDiagnostics": "warn"}
    text = format_text({"summary": {"total": 1, "passed": 0, "failed": 1}, "results": [result]})
    assert text.splitlines()[1:] == [
        "FAIL x: GET http://a.test/", "  timeout /: Handler process exceeded 3000 ms",
        "  showing 1 of 25 schema errors", '  warning: unknown format "x" ignored',
        '  handler output: "log\\n"', '  schema process output: "warn"']


def test_json_report_keeps_the_original_shape_and_formatting():
    text = dumps_report(REPORT) + "\n"
    assert text.startswith('{\n  "version": 1,\n  "scope": "explicit-examples-only",\n  "summary": {\n')
    assert json.loads(text) == REPORT
    assert dumps_report({"sample": "😀\ud800"}) == '{\n  "sample": "😀\\ud800"\n}'


def test_javascript_number_and_json_formatting():
    assert [js_number(value) for value in (0.1, 100.0, 1e21, 1e-7, 123456789012.5, -0.0, 5e-324)] == [
        "0.1", "100", "1e+21", "1e-7", "123456789012.5", "0", "5e-324"]
    assert js_json({"a": [1, 2.5, None, True], "b": "\x01\"/"}) == '{"a":[1,2.5,null,true],"b":"\\u0001\\"/"}'


def test_atomic_report_replacement_keeps_old_artifacts_on_failure_and_cleans_temporary_files(tmp_path):
    report = tmp_path / "report.json"
    report.write_text("previous")
    write_report(report, '{"summary":"new"}\n')
    assert json.loads(report.read_text()) == {"summary": "new"}
    assert os.stat(report).st_mode & 0o777 == 0o600
    existing = tmp_path / "untouched"
    existing.mkdir()
    (existing / "previous.json").write_text("preserved")
    with pytest.raises(OSError):
        write_report(existing, "will not replace directory")
    assert (existing / "previous.json").read_text() == "preserved"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["report.json", "untouched"]
