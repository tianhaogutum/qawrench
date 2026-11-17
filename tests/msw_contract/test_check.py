"""check(): explicit cases, response selection, failures, samples and option validation."""
import re

import pytest

from qawrench.msw_contract import CheckError, check, format_text, normalize_cases, select_media

from .support import (CONTRACT, FIXTURES, HANDLERS, ROOT, UPSTREAM, error_result, example, fake_runner,
                      json_response, requires_msw, requires_node)

OPTIONS = {"handlers_path": HANDLERS, "schema_path": CONTRACT, "cwd": ROOT}
BRANCHES = ["ok", "stale", "missing", "additional", "status", "media", "missing-type", "malformed", "nullable",
            "isolation"]


def _branch_cases():
    cases = [example(scenario) for scenario in BRANCHES]
    cases.append(example("post", method="POST", json={"name": "Milo"}))
    cases.append(example("isolation", id="isolation-again"))
    return cases


def _assert_branches(report):
    by_id = {result["id"]: result for result in report["results"]}
    for case_id in ("ok", "nullable", "isolation", "isolation-again", "post"):
        assert by_id[case_id]["ok"] is True, by_id[case_id]
    assert by_id["stale"]["issues"][0]["path"] == "/id"
    assert by_id["stale"]["issues"][0]["sample"] == '"1"'
    assert by_id["missing"]["issues"][0]["path"] == "/name"
    assert by_id["missing"]["issues"][0]["missing"] is True
    assert by_id["additional"]["issues"][0]["path"] == "/oldField"
    assert by_id["status"]["issues"][0]["code"] == "status"
    assert by_id["media"]["issues"][0]["code"] == "media-type"
    assert by_id["missing-type"]["issues"][0]["code"] == "media-type"
    assert by_id["malformed"]["issues"][0]["code"] == "invalid-json"
    assert re.search(r'schema /id: must be integer; actual "1"', format_text(report))


# ---------------------------------------------------------------------------
# Synthetic runner output: the Python checks without Node


def test_synthetic_branches_check_fields_statuses_media_json_nullable_and_write_only(fake_runner):
    report = check(**OPTIONS, cases=_branch_cases())
    _assert_branches(report)
    assert report["version"] == 1 and report["scope"] == "explicit-examples-only"
    assert report["summary"] == {"total": 12, "passed": 5, "failed": 7}
    stale = next(result for result in report["results"] if result["id"] == "stale")
    assert list(stale) == ["id", "request", "ok", "operation", "statusKey", "mediaType", "bodyValidation",
                           "issueCount", "issuesTruncated", "issues", "response"]
    assert stale["operation"] == "GET /pets/{id}" and stale["statusKey"] == "200"
    assert stale["issues"][0] == {"code": "schema", "path": "/id", "message": "must be integer",
                                  "keyword": "type", "schemaPath": stale["issues"][0]["schemaPath"],
                                  "sample": '"1"', "sampleTruncated": False}
    assert stale["response"] == {"status": 200, "contentType": "application/json", "bytes": 24,
                                 "sample": '{"id":"1","name":"Milo"}', "sampleTruncated": False}
    post = next(example for example, _ in fake_runner.seen if example["id"] == "post")
    assert post["body"] == '{"name":"Milo"}' and post["headers"] == {"content-type": "application/json"}


def test_synthetic_vendor_json_status_ranges_default_text_wildcard_and_no_content(fake_runner):
    report = check(**OPTIONS, cases=[{"id": path, "url": f"http://example.test/{path}"}
                                     for path in ("problem", "text", "empty")])
    problem, text, empty = report["results"]
    assert problem["statusKey"] == "4XX" and problem["issues"][0]["path"] == "/title"
    assert text["statusKey"] == "default" and text["ok"] is True
    assert empty["ok"] is True and empty["bodyValidation"] == "no-content"
    assert empty["response"]["contentType"] is None


def test_synthetic_runner_failures_stay_explicit_and_do_not_abort_later_cases(fake_runner):
    codes = ["handler-error", "timeout", "passthrough", "body-limit", "log-limit", "network-error",
             "unhandled-request", "invalid-status"]
    fake_runner.results.update({code: error_result(code, diagnostics="x" if code == "log-limit" else None)
                                for code in codes})
    report = check(**OPTIONS, cases=[example(code) for code in codes] + [example("ok")])
    assert [result["issues"][0]["code"] for result in report["results"][:-1]] == codes
    assert report["results"][0]["issues"][0] == {"path": "", "code": "handler-error",
                                                  "message": "synthetic failure"}
    assert report["results"][4]["diagnostics"] == "x"
    assert report["results"][-1]["ok"] is True
    assert "response" not in report["results"][0]


def test_synthetic_sample_capture_is_bounded_or_disabled(fake_runner):
    bounded = check(**OPTIONS, sample_bytes=5, cases=[example("ok")])
    assert bounded["results"][0]["response"]["sample"] == '{"id"'
    assert bounded["results"][0]["response"]["sampleTruncated"] is True
    disabled = check(**OPTIONS, sample_bytes=0, cases=[example("stale")])
    assert disabled["results"][0]["response"]["sample"] == ""
    assert "sample" not in disabled["results"][0]["issues"][0]
    assert "sampleTruncated" not in disabled["results"][0]["issues"][0]
    unicode = check(**OPTIONS, sample_bytes=19, cases=[example("unicode")])
    sample = unicode["results"][0]["response"]["sample"]
    assert len(sample.encode()) <= 19 and "\ufffd" not in sample


def test_synthetic_relative_cases_use_the_explicit_base_url(fake_runner):
    fake_runner.results["relative"] = json_response({"id": 1, "name": "Milo"})
    report = check(**OPTIONS, base_url="https://example.test/", cases=[{"id": "relative", "url": "/pets/1"}])
    assert report["results"][0]["request"]["url"] == "https://example.test/pets/1"
    assert report["results"][0]["ok"] is True
    assert fake_runner.seen[0][1]["base_url"] == "https://example.test/"


def test_synthetic_unknown_operation_and_api_root(fake_runner):
    fake_runner.results["elsewhere"] = json_response({"id": 1, "name": "Milo"})
    report = check(**OPTIONS, api_root="/v1", cases=[{"id": "elsewhere", "url": "http://example.test/pets/1"}])
    assert report["results"][0]["issues"][0]["code"] == "operation-not-found"


def test_schema_configuration_errors_exit_before_any_case(fake_runner, tmp_path):
    broken = tmp_path / "swagger.yaml"
    broken.write_text("swagger: '2.0'\ninfo: {title: t, version: '1'}\npaths: {}\n", encoding="utf-8")
    with pytest.raises(CheckError, match="schema-config: Only OpenAPI 3.0.x and 3.1.x are supported"):
        check(**{**OPTIONS, "schema_path": broken}, cases=[example("ok")])
    assert fake_runner.seen == []


# ---------------------------------------------------------------------------
# Input and option validation (no Node needed)


@pytest.mark.parametrize("data", [
    [], {}, [{"url": "file:///etc/passwd"}], [{"url": "/pets"}], [{"url": "https://a.test", "unexpected": True}],
    [{"id": "x", "url": "https://a.test"}, {"id": "x", "url": "https://a.test"}],
    [{"url": "https://a.test", "json": {}, "body": ""}], [{"url": "https://a.test", "method": "GET", "body": "{}"}],
])
def test_invalid_case_input_is_rejected_before_handler_execution(data):
    with pytest.raises(CheckError):
        normalize_cases(data)


def test_case_normalization():
    [case] = normalize_cases([{"url": "pets", "method": "post", "json": {"name": "Milo"}}], "https://a.test/v1/")
    assert case == {"id": "case-1", "method": "POST", "url": "https://a.test/v1/pets",
                    "headers": {"content-type": "application/json"}, "body": '{"name":"Milo"}'}
    with pytest.raises(CheckError, match="IDs"):
        normalize_cases([{"id": None, "url": "https://a.test"}])
    with pytest.raises(CheckError, match="method"):
        normalize_cases([{"method": None, "url": "https://a.test"}])
    [case] = normalize_cases([{"url": "HTTPS://A.Test:443/a/../b c?q=1 2", "headers": [["X-B", " 1 "], ["x-a", "2"]]}])
    assert case["url"] == "https://a.test/b%20c?q=1%202"
    assert case["headers"] == {"x-a": "2", "x-b": "1"}
    with pytest.raises(CheckError, match="Invalid URL"):
        normalize_cases([{"url": "/pets"}])
    with pytest.raises(CheckError, match="credentials"):
        normalize_cases([{"url": "https://user:secret@a.test/"}])
    with pytest.raises(CheckError, match="invalid header name"):
        normalize_cases([{"url": "https://a.test", "headers": {"bad name": "x"}}])


def test_runtime_api_rejects_missing_or_conflicting_case_sources_and_invalid_options():
    with pytest.raises(CheckError, match="exactly one"):
        check(**OPTIONS)
    with pytest.raises(CheckError, match="exactly one"):
        check(**OPTIONS, cases=[], cases_path="examples/msw-contract/cases.json")
    with pytest.raises(CheckError, match="non-empty array"):
        check(**OPTIONS, cases=None)
    with pytest.raises(CheckError, match="api_root"):
        check(**OPTIONS, cases=[{"url": "https://a.test"}], api_root=None)
    with pytest.raises(CheckError, match="export_name"):
        check(**OPTIONS, cases=[{"url": "https://a.test"}], export_name="")
    with pytest.raises(CheckError, match="timeout_ms must be an integer between 1 and 120000"):
        check(**OPTIONS, cases=[{"url": "https://a.test"}], timeout_ms=float("nan"))


def test_media_matching_chooses_exact_then_type_wildcard_then_global_wildcard():
    content = {"*/*": {}, "application/*": {}, "Application/JSON": {}}
    assert select_media(content, "application/json; charset=UTF-8")["key"] == "Application/JSON"
    assert select_media(content, "application/problem+json")["key"] == "application/*"
    assert select_media(content, "text/plain")["key"] == "*/*"
    assert select_media(content, "broken") is None


def test_media_parameters_select_the_right_version_and_respect_charset_names():
    content = {"application/json;version=1": {}, "application/json": {}, "application/json;version=2": {}}
    assert select_media(content, "application/json;version=2;charset=UTF-8")["key"] == "application/json;version=2"
    assert select_media(content, "application/json;version=3")["key"] == "application/json"
    assert select_media({"application/json;version=1": {}}, "application/json;version=2") is None
    assert select_media({"text/plain;charset=utf-8": {}}, "Text/Plain;Charset=UTF-8")["key"] == "text/plain;charset=utf-8"
    assert len(select_media({"application/json": {}, "Application/JSON": {}}, "application/json")["ambiguousKeys"]) == 2
    assert select_media({"*/*": {}}, "application/*") is None
    assert select_media({"text/plain;charset=iso-8859-1": {}}, "text/plain; charset=latin1") is not None


# ---------------------------------------------------------------------------
# Real MSW handlers through the bundled Node runner


@requires_node("@msw/source", "yaml")
def test_real_oai_petstore_yaml_and_real_msw_source_generated_handlers():
    report = check(handlers_path=FIXTURES / "generated.mjs", schema_path=UPSTREAM / "petstore-3.0.yaml",
                   api_root="/v1", cwd=ROOT, cases=[{"url": "http://petstore.swagger.io/v1/pets/1"},
                                                    {"url": "http://petstore.swagger.io/v1/pets"}])
    assert report["summary"]["failed"] == 0, report
    assert report["results"][0]["response"]["status"] == 200


@requires_node("openapi-msw")
def test_existing_openapi_msw_response_helper_handlers_are_audited_without_rewriting_them():
    report = check(handlers_path=FIXTURES / "openapi-msw.mjs", schema_path=UPSTREAM / "petstore-3.0.yaml",
                   api_root="/v1", cwd=ROOT,
                   cases=[{"id": "valid-helper", "url": "http://petstore.swagger.io/v1/pets/1"},
                          {"id": "stale-helper", "url": "http://petstore.swagger.io/v1/pets/stale"}])
    assert report["results"][0]["ok"] is True, report
    assert report["results"][1]["issues"][0]["path"] == "/id"


@requires_msw
def test_actual_branches_check_fields_statuses_media_json_nullable_write_only_and_request_bodies():
    _assert_branches(check(**OPTIONS, cases=_branch_cases()))


@requires_msw
def test_vendor_json_status_ranges_default_response_text_wildcard_and_no_content():
    report = check(**OPTIONS, cases=[{"id": path, "url": f"http://example.test/{path}"}
                                     for path in ("problem", "text", "empty")])
    assert report["results"][0]["statusKey"] == "4XX"
    assert report["results"][0]["issues"][0]["path"] == "/title"
    assert report["results"][1]["statusKey"] == "default"
    assert report["results"][1]["ok"] is True
    assert report["results"][2]["ok"] is True


@requires_msw
def test_failures_remain_explicit_bounded_and_do_not_abort_subsequent_cases():
    scenarios = ["throw", "timeout", "busy", "passthrough", "network", "http-network", "write", "oversized", "logs",
                 "error", "undefined", "ok"]
    report = check(**OPTIONS, timeout_ms=1000, max_body_bytes=1024, cases=[example(s) for s in scenarios])
    codes = [result["issues"][0]["code"] if result["issues"] else None for result in report["results"]]
    assert codes == ["handler-error", "timeout", "timeout", "passthrough", "handler-error", "handler-error",
                     "handler-error", "body-limit", "log-limit", "network-error", "unhandled-request", None]
    assert re.search(r"(?i)permission|Access to this API has been restricted",
                     report["results"][6]["issues"][0]["message"])
    assert report["results"][-1]["ok"] is True
    assert not (FIXTURES / "forbidden.txt").exists()


@requires_msw
def test_sample_capture_is_bounded_or_disabled():
    bounded = check(**OPTIONS, sample_bytes=5, cases=[example("ok")])
    assert bounded["results"][0]["response"]["sample"] == '{"id"'
    assert bounded["results"][0]["response"]["sampleTruncated"] is True
    disabled = check(**OPTIONS, sample_bytes=0, cases=[example("stale")])
    assert disabled["results"][0]["response"]["sample"] == ""
