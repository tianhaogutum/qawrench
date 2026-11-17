"""OpenAPI loading and response-schema checks (pure Python: synthetic responses, no Node)."""
import itertools
import json
import re

import pytest

from qawrench.msw_contract import CheckError, check, check_response, load_contract

from .support import FIXTURES, ROOT, UPSTREAM, requires_msw

_ABSENT = object()


@pytest.fixture
def contract_for(tmp_path):
    counter = itertools.count(1)

    def make(schema=_ABSENT, *, version="3.1.0", components=None, media_type="application/json",
             media_extras=None, extra=None):
        media = {} if schema is _ABSENT else {"schema": schema}
        media.update(media_extras or {})
        document = {"openapi": version, "info": {"title": "Regression", "version": "1"},
                    "paths": {"/value": {"get": {"responses": {"200": {"description": "Value",
                                                                       "content": {media_type: media}}}}}},
                    "components": components or {}, **(extra or {})}
        path = tmp_path / f"schema-{next(counter)}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return load_contract(path)

    make.directory = tmp_path
    return make


def validate_body(contract, body, headers=None):
    text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    return check_response(contract, {"method": "GET", "url": "http://example.test/value"},
                          {"status": 200, "headers": headers or {"content-type": "application/json"},
                           "body": text, "bytes": len(text.encode())})


@requires_msw
def test_real_upstream_openapi_generator_31_petstore_has_correct_json_response_selection():
    report = check(handlers_path=FIXTURES / "upstream31.mjs", schema_path=UPSTREAM / "petstore-3.1.yaml",
                   api_root="/v2", cwd=ROOT, cases=[{"id": "good", "url": "http://petstore.swagger.io/v2/pet/1"},
                                                    {"id": "stale", "url": "http://petstore.swagger.io/v2/pet/stale"}])
    assert report["results"][0]["ok"] is True, report
    assert report["results"][1]["issues"][0]["path"] == "/id"


def test_upstream_openapi_generator_31_petstore_selects_the_json_entry_without_node():
    contract = load_contract(UPSTREAM / "petstore-3.1.yaml", api_root="/v2")
    example = {"method": "GET", "url": "http://petstore.swagger.io/v2/pet/1"}
    good = '{"id":1,"name":"Milo","photoUrls":[]}'
    result = check_response(contract, example, {"status": 200, "headers": {"content-type": "application/json"},
                                                 "body": good, "bytes": len(good)})
    assert result["issues"] == [] and result["mediaType"] == "application/json", result
    stale = '{"id":"1","name":"Milo","photoUrls":[]}'
    result = check_response(contract, example, {"status": 200, "headers": {"content-type": "application/json"},
                                                 "body": stale, "bytes": len(stale)})
    assert result["issues"][0]["path"] == "/id"
    xml = check_response(contract, example, {"status": 200, "headers": {"content-type": "application/xml"},
                                              "body": "<Pet/>", "bytes": 6})
    assert xml["issues"][0]["code"] == "unsupported-media"


def test_upstream_oai_30_petstore_routes_through_api_root_and_default_responses():
    contract = load_contract(UPSTREAM / "petstore-3.0.yaml", api_root="/v1")
    missing = '{"code":404,"message":"No such pet"}'
    result = check_response(contract, {"method": "GET", "url": "http://petstore.swagger.io/v1/pets/missing"},
                            {"status": 404, "headers": {"content-type": "application/json"},
                             "body": missing, "bytes": len(missing)})
    assert result["statusKey"] == "default" and result["issues"] == []
    created = check_response(contract, {"method": "POST", "url": "http://petstore.swagger.io/v1/pets"},
                             {"status": 201, "headers": {}, "body": "", "bytes": 0})
    assert created["bodyValidation"] == "no-content" and created["issues"] == []
    assert created["operation"] == "POST /pets"


@pytest.mark.parametrize("version", ["3.0.4", "3.1.1", "3.1.2"])
def test_supported_patch_versions_still_reject_stale_field_types(contract_for, version):
    contract = contract_for({"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}},
                            version=version)
    assert validate_body(contract, {"id": 1})["issues"] == []
    assert validate_body(contract, {"id": "1"})["issues"][0]["path"] == "/id"


@pytest.mark.parametrize("version", ["3.0.3", "3.1.0"])
def test_recursive_component_references_retain_nested_validation(contract_for, version):
    node = {"type": "object", "required": ["name"], "additionalProperties": False,
            "properties": {"name": {"type": "string"}, "children": {"type": "array",
                                                                     "items": {"$ref": "#/components/schemas/Node"}}}}
    contract = contract_for({"$ref": "#/components/schemas/Node"}, version=version,
                            components={"schemas": {"Node": node}})
    assert validate_body(contract, {"name": "root", "children": [{"name": "leaf"}]})["issues"] == []
    bad = validate_body(contract, {"name": "root", "children": [{"name": 42}]})
    assert bad["issues"][0]["path"] == "/children/0/name", bad


def test_json_syntax_is_checked_even_when_the_media_entry_has_no_schema(contract_for):
    contract = contract_for()
    valid = validate_body(contract, {"arbitrary": True})
    assert valid["bodyValidation"] == "no-schema" and valid["issues"] == []
    malformed = check_response(contract, {"method": "GET", "url": "http://example.test/value"},
                               {"status": 200, "headers": {"content-type": "application/json"},
                                "body": "{broken", "bytes": 7})
    assert malformed["issues"][0]["code"] == "invalid-json"


def test_unknown_schema_formats_surface_as_structured_warnings(contract_for):
    contract = contract_for({"type": "string", "format": "my-team-id"})
    for _ in range(2):
        result = validate_body(contract, "valid structure")
        assert result["issues"] == []
        assert re.search(r"unknown format.*my-team-id", result["warnings"][0]["message"])
        assert result["warnings"][0]["code"] == "schema-warning"


def test_field_names_use_json_pointer_escaping_and_truncation_reports_the_full_error_count(contract_for):
    required = [f"field/{i}~old" for i in range(25)]
    contract = contract_for({"type": "object", "required": required,
                             "properties": {name: {"type": "string"} for name in required}})
    result = validate_body(contract, {})
    assert len(result["issues"]) == 20
    assert result["issueCount"] == 25
    assert result["issuesTruncated"] is True
    assert result["issues"][0]["path"] == "/field~10~0old"
    assert result["issues"][0]["message"] == "must have required property 'field/0~old'"
    assert result["issues"][0]["missing"] is True


@pytest.mark.parametrize("version", ["3.0.3", "3.1.0"])
def test_required_fields_cannot_be_satisfied_by_inherited_property_names(contract_for, version):
    contract = contract_for({"type": "object", "required": ["constructor", "toString", "__proto__"]},
                            version=version)
    assert len(validate_body(contract, {})["issues"]) == 3
    present = json.loads('{"constructor":"value","toString":"value","__proto__":"value"}')
    assert validate_body(contract, present)["issues"] == []


def test_parameterized_media_keys_with_uri_fragments_retain_the_right_schema_pointer(contract_for):
    media = 'application/json;profile="https://example.test/profiles/pet#v2"'
    contract = contract_for({"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}},
                            media_type=media)
    valid = validate_body(contract, {"id": 1}, {"content-type": media})
    assert valid["issues"] == [], valid
    stale = validate_body(contract, {"id": "1"}, {"content-type": media})
    assert stale["issues"][0]["path"] == "/id", stale


def test_31_ref_siblings_remain_conjunctive_when_their_type_conflicts_with_the_target(contract_for):
    contract = contract_for({"$ref": "#/components/schemas/Text", "type": "integer"},
                            components={"schemas": {"Text": {"type": "string"}}})
    assert validate_body(contract, 42)["issues"]
    assert validate_body(contract, "text")["issues"]


def test_31_external_ref_siblings_remain_conjunctive_during_bundling(contract_for):
    (contract_for.directory / "text.json").write_text(json.dumps({"type": "string", "maxLength": 3}))
    contract = contract_for({"$ref": "./text.json", "maxLength": 10})
    assert validate_body(contract, "abc")["issues"] == []
    assert validate_body(contract, "abcdef")["issues"]


@pytest.mark.parametrize("literal", ["https://example.test/not-a-real-reference", "#/not/a/real/pointer"])
@pytest.mark.parametrize("extras", ["example", "examples"])
def test_literal_ref_fields_in_singular_and_named_examples_are_data(contract_for, literal, extras):
    media_extras = {"example": {"$ref": literal}} if extras == "example" else \
        {"examples": {"saved": {"value": {"$ref": literal}}}}
    contract = contract_for({"type": "object"}, media_extras=media_extras)
    assert validate_body(contract, {})["issues"] == []


def test_31_static_schema_anchors_are_resolved_by_the_bundler(contract_for):
    pet = {"$anchor": "pet", "type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}
    contract = contract_for({"$ref": "#pet"}, components={"schemas": {"Pet": pet}})
    assert validate_body(contract, {"id": 1})["issues"] == []
    assert validate_body(contract, {"id": "1"})["issues"][0]["path"] == "/id"


def test_2020_12_tuple_boolean_schema_and_unevaluated_properties_constraints_are_enforced(contract_for):
    tuple_contract = contract_for({"type": "array", "prefixItems": [{"type": "string"}, {"type": "integer"}],
                                   "items": False, "minItems": 2})
    assert validate_body(tuple_contract, ["hello", 1])["issues"] == []
    assert validate_body(tuple_contract, ["hello", "1"])["issues"][0]["path"] == "/1"
    too_long = validate_body(tuple_contract, ["hello", 1, True])["issues"][0]
    assert too_long["keyword"] == "items" and too_long["message"] == "must NOT have more than 2 items"
    closed = contract_for({"type": "object", "allOf": [{"properties": {"name": {"type": "string"}}}],
                           "unevaluatedProperties": False})
    assert validate_body(closed, {"name": "Milo"})["issues"] == []
    stale = validate_body(closed, {"stale": True})["issues"][0]
    assert stale["keyword"] == "unevaluatedProperties" and stale["path"] == "/stale"
    never = contract_for(False)
    assert validate_body(never, {})["issues"][0]["keyword"] == "false schema"


def test_30_exclusive_bounds_and_31_null_union_preserve_native_dialect_semantics(contract_for):
    old = contract_for({"type": "number", "minimum": 1, "exclusiveMinimum": True}, version="3.0.3")
    issue = validate_body(old, 1)["issues"][0]
    assert issue["keyword"] == "minimum" and issue["message"] == "must be > 1"
    assert validate_body(old, 2)["issues"] == []
    modern = contract_for({"type": ["object", "null"], "required": ["id", "password"],
                           "properties": {"id": {"type": "integer"}, "password": {"type": "string", "writeOnly": True}}})
    assert validate_body(modern, None)["issues"] == []
    assert validate_body(modern, {"id": 1})["issues"] == []
    assert validate_body(modern, {"id": "1"})["issues"][0]["path"] == "/id"


def test_30_nullable_integral_numbers_and_formats(contract_for):
    contract = contract_for({"type": "object", "required": ["id", "name"], "properties": {
        "id": {"type": "integer", "format": "int32"}, "name": {"type": "string", "nullable": True},
        "born": {"type": "string", "format": "date"}, "kind": {"type": "string", "enum": ["cat"], "nullable": True}}},
        version="3.0.3")
    assert validate_body(contract, {"id": 1.0, "name": None, "born": "2024-02-29", "kind": None})["issues"] == []
    issues = validate_body(contract, {"id": 2 ** 31, "name": "Milo", "born": "2023-02-29"})["issues"]
    assert {(issue["path"], issue["keyword"]) for issue in issues} == {("/id", "format"), ("/born", "format")}
    assert issues[0]["message"].startswith("must match format")


@pytest.mark.parametrize("version", ["3.0.3", "3.1.0"])
def test_required_write_only_property_references_need_not_appear_in_responses(contract_for, version):
    components = {"schemas": {"Password": {"type": "string", "writeOnly": True}}}
    contract = contract_for({"type": "object", "required": ["password"],
                             "properties": {"password": {"$ref": "#/components/schemas/Password"}}},
                            version=version, components=components)
    assert validate_body(contract, {})["issues"] == []
    composed = contract_for({"type": "object", "required": ["password"], "properties": {"password": {
        "allOf": [{"$ref": "#/components/schemas/Password"}, {"maxLength": 40}]}}},
        version=version, components=components)
    assert validate_body(composed, {})["issues"] == []
    read_only = contract_for({"type": "object", "required": ["id"],
                              "properties": {"id": {"$ref": "#/components/schemas/ID"}}},
                             version=version, components={"schemas": {"ID": {"type": "integer", "readOnly": True}}})
    assert validate_body(read_only, {})["issues"][0]["path"] == "/id"


def test_response_component_references_and_local_external_schema_files(contract_for):
    (contract_for.directory / "external.json").write_text(json.dumps(
        {"Pet": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}}))
    contract = contract_for({"$ref": "./external.json#/Pet"}, extra={
        "paths": {"/value": {"get": {"responses": {"200": {"$ref": "#/components/responses/Pet"}}}}},
        "components": {"responses": {"Pet": {"description": "Pet", "content": {
            "application/json": {"schema": {"$ref": "./external.json#/Pet"}}}}}}})
    assert validate_body(contract, {"id": 1})["issues"] == []
    assert validate_body(contract, {"id": "1"})["issues"][0]["path"] == "/id"


def test_remote_references_and_unknown_schema_dialects_fail_before_execution(contract_for):
    with pytest.raises(CheckError, match=r"(?i)resolve|resolver|download|Error opening|Unable"):
        contract_for({"$ref": "https://example.test/never.json"})
    with pytest.raises(CheckError, match="Unsupported JSON Schema dialect"):
        contract_for({"type": "string"}, extra={"jsonSchemaDialect": "https://example.test/custom-dialect"})
    with pytest.raises(CheckError, match="not supported"):
        contract_for({"$id": "https://example.test/schema", "type": "string"})


def test_2020_12_response_context_and_unsupported_dialects_are_visited_inside_tuple_and_dependent_schemas(
        contract_for):
    secret = {"type": "object", "required": ["password"], "properties": {"password": {"type": "string",
                                                                                       "writeOnly": True}}}
    tuple_contract = contract_for({"type": "array", "prefixItems": [secret], "items": False})
    assert validate_body(tuple_contract, [{}])["issues"] == []
    dependent = contract_for({"type": "object", "properties": {"enabled": {"type": "boolean"}},
                              "dependentSchemas": {"enabled": secret}})
    assert validate_body(dependent, {"enabled": True})["issues"] == []
    with pytest.raises(CheckError, match="not supported"):
        contract_for({"type": "array", "prefixItems": [{"$id": "https://example.test/nested", "type": "string"}]})


@pytest.mark.parametrize("version", ["3.0.3", "3.1.0"])
def test_recursive_schemas_in_external_files_retain_their_reference_base(contract_for, version):
    (contract_for.directory / "recursive.json").write_text(json.dumps({"Node": {
        "type": "object", "required": ["name"],
        "properties": {"name": {"type": "string"}, "child": {"$ref": "#/Node"}}}}))
    contract = contract_for({"$ref": "./recursive.json#/Node"}, version=version)
    good = validate_body(contract, {"name": "root", "child": {"name": "leaf"}})
    assert good["issues"] == [], good
    bad = validate_body(contract, {"name": "root", "child": {"name": False}})
    assert bad["issues"][0]["path"] == "/child/name", bad


def test_yaml_contracts_use_the_yaml_12_core_schema(tmp_path):
    spec = tmp_path / "openapi.yaml"
    spec.write_text("""\
openapi: 3.0.3
info: {title: YAML, version: '1'}
paths:
  /value:
    get:
      responses:
        200:
          description: ok
          content:
            application/json:
              schema:
                type: object
                properties:
                  day: {type: string, enum: [2024-01-01]}
                  flag: {type: string, enum: [yes, on]}
""", encoding="utf-8")
    contract = load_contract(spec)
    assert validate_body(contract, {"day": "2024-01-01", "flag": "yes"})["issues"] == []
    assert validate_body(contract, {"flag": True})["issues"][0]["path"] == "/flag"
