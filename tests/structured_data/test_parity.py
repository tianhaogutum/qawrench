"""All HTML generated here is synthetic and offline; no real-site claims."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from qawrench.structured_data import ParityError, compare_files
from qawrench.structured_data.cli import main, markdown_report

ROOT = Path(__file__).resolve().parents[2]
BASE = "https://fixture.test/catalog/page"


def document(value=None, *, scripts=None, meta="", body=""):
    if scripts is None:
        scripts = [] if value is None else [json.dumps(value, ensure_ascii=True)]
    return ('<!doctype html><html><head><meta charset="utf-8">' + meta
            + ''.join('\n<script type="application/ld+json">' + script + '</script>' for script in scripts)
            + '</head><body>' + body + '</body></html>')


class FixtureCase(unittest.TestCase):
    def setUp(self):
        (ROOT / ".cache").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / ".cache", prefix="test-")
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.old, self.new = self.directory / "old.html", self.directory / "new.html"
        # Any accidental runtime fetching is a test failure, even for contexts.
        self.network = patch.object(socket, "socket", side_effect=AssertionError("runtime must remain offline"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def compare(self, left, right, **options):
        self.old.write_text(left, encoding="utf-8")
        self.new.write_text(right, encoding="utf-8")
        return compare_files(self.old, self.new, base_url=BASE, **options)

    def failure(self, code, text, **options):
        self.old.write_text(text, encoding="utf-8")
        self.new.write_text(document({"@id": "ok"}), encoding="utf-8")
        with self.assertRaises(ParityError) as raised:
            compare_files(self.old, self.new, base_url=BASE, **options)
        self.assertEqual(raised.exception.code, code)
        return raised.exception


class ExtractionAndComparisonTests(FixtureCase):
    def test_real_extruct_examples(self):
        report = compare_files(ROOT / "examples/structured_data/raw.html", ROOT / "examples/structured_data/rendered.html", base_url="https://fixture.test/")
        self.assertEqual(report["status"], "different")
        self.assertEqual(report["change_count"], 2)
        price, title = report["changes"]
        self.assertEqual((price["entity"], price["path"], price["old"], price["new"]),
                         ("https://fixture.test/fixture/product", "/offers/price", "12.00", "14.00"))
        self.assertEqual(price["old_provenance"]["pointer"], "/@graph/0")
        self.assertEqual(price["old_provenance"]["line"], 8)
        self.assertEqual(title["path"], "/og:title/0")
        self.assertEqual(report["captures"]["old"]["jsonld_entities"], 2)
        self.assertEqual(report["captures"]["old"]["opengraph_values"], 4)

    def test_key_and_entity_reordering_is_equal(self):
        left = {"@context": "https://schema.org", "@graph": [
            {"@id": "a", "offers": {"price": 12, "currency": "EUR"}}, {"@id": "b", "name": "B"}]}
        right = {"@graph": [{"name": "B", "@id": "b"},
                              {"offers": {"currency": "EUR", "price": 12}, "@id": "a"}], "@context": "https://schema.org"}
        self.assertEqual(self.compare(document(left), document(right))["changes"], [])

    def test_arrays_preserve_order_multiplicity_and_report_indices(self):
        report = self.compare(document({"@id": "x", "tags": ["b", "a", "a"]}),
                              document({"@id": "x", "tags": ["a", "b"]}))
        self.assertEqual([(c["kind"], c["path"]) for c in report["changes"]],
                         [("changed", "/tags/0"), ("changed", "/tags/1"), ("removed", "/tags/2")])

    def test_explicit_set_arrays_and_nested_objects(self):
        left = {"@id": "x", "tags": [{"a": 1, "b": 2}, "a", "a"]}
        right = {"@id": "x", "tags": ["a", {"b": 2, "a": 1}]}
        report = self.compare(document(left), document(right), set_valued=("/tags",))
        self.assertEqual(report["changes"], [])
        self.assertEqual(report["policy"]["set_valued"], ["/tags"])

    def test_relative_id_resolves_only_for_pairing_and_html_base_ignored(self):
        report = self.compare(document({"@id": "../item", "name": "x"}, meta='<base href="https://elsewhere.test/">'),
                              document({"@id": "https://fixture.test/item", "name": "x"}))
        self.assertEqual(report["change_count"], 1)
        self.assertEqual(report["changes"][0]["entity"], "https://fixture.test/item")
        self.assertEqual(report["changes"][0]["path"], "/@id")

    def test_blank_node_ids_and_fragments(self):
        for identity, expected in [("_:node", "_:node"), ("#p", BASE + "#p")]:
            report = self.compare(document({"@id": identity, "name": "a"}), document({"@id": identity, "name": "b"}))
            self.assertEqual(report["changes"][0]["entity"], expected)

    def test_configured_identity_no_fallback(self):
        report = self.compare(document({"sku": "SKU-12", "price": "1"}),
                              document({"price": "2", "sku": "SKU-12"}), identity_field="sku")
        self.assertEqual(report["changes"][0]["entity"], "SKU-12")
        self.assertEqual(report["changes"][0]["path"], "/price")
        self.failure("missing_identity", document({"@id": "id-only"}), identity_field="sku")

    def test_added_removed_entities_properties_and_null(self):
        left = [{"@id": "gone", "name": "old"}, {"@id": "same", "removed": None, "value": None}]
        right = [{"@id": "new", "name": "new"}, {"@id": "same", "added": None, "value": "now"}]
        report = self.compare(document(left), document(right))
        kinds = {c["kind"] for c in report["changes"]}
        self.assertEqual(kinds, {"added_entity", "removed_entity", "added", "removed", "changed"})
        removed = next(c for c in report["changes"] if c["path"] == "/removed")
        self.assertIsNone(removed["old"])
        self.assertNotIn("new", removed)

    def test_json_pointer_escaping(self):
        report = self.compare(document({"@id": "x", "a/b~c": {"": 1}}),
                              document({"@id": "x", "a/b~c": {"": 2}}))
        self.assertEqual(report["changes"][0]["path"], "/a~1b~0c/")

    def test_nested_references_are_compared_without_identity_resolution(self):
        report = self.compare(document({"@id": "x", "seller": {"@id": "#s"}}),
                              document({"@id": "x", "seller": {"@id": BASE + "#s"}}))
        self.assertEqual(report["changes"][0]["path"], "/seller/@id")
        self.assertEqual(report["changes"][0]["old"], "#s")

    def test_json_numeric_and_boolean_types_are_not_coerced(self):
        for left, right in [(1, True), (1, 1.0), ("12", 12), ([], {})]:
            report = self.compare(document({"@id": "x", "value": left}), document({"@id": "x", "value": right}))
            self.assertEqual(report["change_count"], 1)
            self.assertEqual(report["changes"][0]["path"], "/value")

    def test_graph_context_and_empty_wrapper_preserved(self):
        for graph in ([], [{"@id": "x", "@context": {"name": "local"}}]):
            report = self.compare(document({"@context": "https://context.invalid/a", "@graph": graph}),
                                  document({"@context": "https://context.invalid/b", "@graph": graph}))
            self.assertEqual(report["change_count"], 1)
            self.assertEqual(report["changes"][0]["scope"], "graph_metadata")
            self.assertEqual(report["changes"][0]["path"], "/0/@context")

    def test_multiple_scripts_and_root_arrays_provenance(self):
        left = document(scripts=[json.dumps({"@id": "a"}), json.dumps([{"@id": "b", "name": "old"}])])
        right = document([{"@id": "a"}, {"@id": "b", "name": "new"}])
        report = self.compare(left, right)
        source = report["changes"][0]["old_provenance"]
        self.assertEqual((source["script_index"], source["pointer"]), (1, "/0"))

    def test_opengraph_duplicates_order_and_missing_properties(self):
        left = '<meta property="og:image" content="a"><meta property="og:image" content="b"><meta property="og:title" content="title">'
        right = '<meta property="og:image" content="b"><meta property="og:image" content="a"><meta property="og:description" content="new">'
        report = self.compare(document(meta=left), document(meta=right))
        paths = {c["path"] for c in report["changes"] if c["scope"] == "properties"}
        self.assertEqual(paths, {"/og:image/0", "/og:image/1", "/og:title", "/og:description"})
        removed = next(c for c in report["changes"] if c["path"] == "/og:title")
        self.assertEqual(removed["old"], ["title"])

    def test_opengraph_property_order_is_ignored_but_namespaces_retained(self):
        a = '<meta property="og:title" content="A"><meta property="og:type" content="article">'
        b = '<meta property="og:type" content="article"><meta property="og:title" content="A">'
        self.assertEqual(self.compare(document(meta=a), document(meta=b))["changes"], [])
        left = document(meta='<meta property="custom:title" content="A">').replace('<html>', '<html prefix="custom: https://custom.invalid/a#">')
        right = left.replace('https://custom.invalid/a#', 'https://custom.invalid/b#')
        changes = self.compare(left, right)["changes"]
        self.assertEqual([(c["scope"], c["path"]) for c in changes], [("namespaces", "/custom")])

    def test_syntax_selection_and_empty_valid_capture_counts(self):
        invalid_json = document(scripts=['{"broken":'])
        self.assertEqual(self.compare(invalid_json, invalid_json, syntaxes=("opengraph",))["status"], "equal")
        report = self.compare(document(), document())
        self.assertEqual(report["status"], "equal")
        self.assertEqual(report["captures"]["old"]["jsonld_entities"], 0)
        self.assertEqual(report["captures"]["old"]["opengraph_values"], 0)

    def test_utf8_bom_unicode_and_no_script_execution(self):
        sentinel = self.directory / "executed"
        body = f'<script>throw new Error("{sentinel}");</script>'
        self.assertEqual(self.compare('\ufeff' + document({"@id": "x", "name": "雪"}, body=body),
                                      document({"@id": "x", "name": "雪"}))["status"], "equal")
        self.assertFalse(sentinel.exists())


class NormalizationPolicyTests(FixtureCase):
    def test_exact_string_policies_and_reported_normalized_values(self):
        left = {"@id": "x", "name": "  Tea\n mug ", "brand": "Straße",
                "a/b~c": "  padded  ", "untouched": " A ", "description": " old "}
        right = {"@id": "x", "name": "Tea mug", "brand": "STRASSE",
                 "a/b~c": "padded", "untouched": "A", "description": " new "}
        policy = {"/name": "collapse-whitespace", "/brand": "casefold",
                  "/a~1b~0c": "trim", "/description": "trim"}
        self.assertEqual(self.compare(document(left), document(right))["change_count"], 5)
        report = self.compare(document(left), document(right), normalize=policy)
        self.assertEqual(report["policy"]["normalize"], policy)
        self.assertEqual([(c["path"], c["old"], c["new"]) for c in report["changes"]],
                         [("/description", "old", "new"), ("/untouched", " A ", "A")])
        self.assertEqual(report["changes"][0]["old_provenance"]["script_index"], 0)

    def test_absent_null_and_empty_values_remain_distinct(self):
        values = [{}, {"value": None}, {"value": ""}, {"value": []}, {"value": {}}]
        for index, left in enumerate(values):
            for right in values[index + 1:]:
                with self.subTest(left=left, right=right):
                    report = self.compare(document({"@id": "x", "name": " x ", **left}),
                                          document({"@id": "x", "name": "x", **right}),
                                          normalize={"/name": "trim"})
                    self.assertEqual(report["change_count"], 1)
                    change = report["changes"][0]
                    self.assertEqual(change["path"], "/value")
                    self.assertEqual("old" in change, "value" in left)
                    self.assertEqual(change["new"], right["value"])
                    if "value" in left:
                        self.assertEqual(change["old"], left["value"])
        for left, right, kind in [({}, {"value": None}, "added"),
                                  ({"value": None}, {}, "removed"),
                                  ({"value": None}, {"value": ""}, "changed")]:
            report = self.compare(document({"@id": "x", **left}), document({"@id": "x", **right}),
                                  normalize={"/value": "trim"})
            self.assertEqual(report["changes"][0]["kind"], kind)

    def test_string_modes_are_independent_and_do_not_coerce(self):
        for mode, left, right, equal in [
                ("trim", " a  b ", "a  b", True),
                ("trim", "a  b", "a b", False),
                ("collapse-whitespace", " A\u00a0\nB ", "A B", True),
                ("collapse-whitespace", "A", "a", False),
                ("casefold", "Straße", "STRASSE", True),
                ("casefold", " A ", "a", False),
                ("trim", None, None, True), ("trim", " \n", "", True)]:
            with self.subTest(mode=mode, left=left, right=right):
                report = self.compare(document({"@id": "x", "v": left}),
                                      document({"@id": "x", "v": right}), normalize={"/v": mode})
                self.assertEqual(report["status"] == "equal", equal)

    def test_string_policies_apply_before_set_sorting_and_to_added_entities(self):
        report = self.compare(document({"@id": "x", "tags": [" A ", " B "]}),
                              document({"@id": "x", "tags": ["B", "A"]}),
                              set_valued=("/tags",), normalize={"/tags/0": "trim", "/tags/1": "trim"})
        self.assertEqual(report["changes"], [])
        report = self.compare(document(), document({"@id": "x", "name": " x "}),
                              normalize={"/name": "trim"})
        self.assertEqual(report["changes"][0]["new"]["name"], "x")

    def test_normalization_does_not_touch_graph_metadata_or_opengraph(self):
        left = {"@context": " Context ", "@graph": [{"@id": "x", "@context": " Context "}]}
        right = {"@context": "Context", "@graph": [{"@id": "x", "@context": "Context"}]}
        report = self.compare(document(left, meta='<meta property="og:title" content=" Title ">'),
                              document(right, meta='<meta property="og:title" content="Title">'),
                              normalize={"/@context": "trim"})
        self.assertEqual([(c["scope"], c["path"]) for c in report["changes"]],
                         [("graph_metadata", "/0/@context"), ("properties", "/og:title/0")])

    def test_shipped_normalization_example(self):
        report = compare_files(ROOT / "examples/structured_data/normalization-old.html", ROOT / "examples/structured_data/normalization-new.html",
                               base_url=BASE, normalize={"/name": "collapse-whitespace", "/brand": "casefold"},
                               set_valued=("/tags",))
        self.assertEqual([(c["kind"], c["path"]) for c in report["changes"]],
                         [("changed", "/description"), ("removed", "/note"),
                          ("changed", "/steps/@list/0"), ("changed", "/steps/@list/1"),
                          ("removed", "/steps/@list/2"), ("added", "/summary")])

    def test_list_order_and_duplicates_survive_other_set_policies(self):
        left = {"@id": "x", "tags": ["b", "a", "a"], "steps": {"@list": ["b", "a", "a"]}}
        right = {"@id": "x", "tags": ["a", "b"], "steps": {"@list": ["a", "b"]}}
        report = self.compare(document(left), document(right), set_valued=("/tags",))
        self.assertEqual([(c["kind"], c["path"]) for c in report["changes"]],
                         [("changed", "/steps/@list/0"), ("changed", "/steps/@list/1"),
                          ("removed", "/steps/@list/2")])
        with self.assertRaises(ParityError) as raised:
            self.compare(document(left), document(right), set_valued=("/steps/@list",))
        self.assertEqual(raised.exception.code, "invalid_config")
        self.assertIn("@list", str(raised.exception))

    def test_invalid_policies_fail_explicitly(self):
        for policy in [[], {"v": "trim"}, {"/bad~2": "trim"}, {"/v": "unknown"},
                       {"/v": []}, {"/missing": "trim"}, {"/@id": "trim"}]:
            with self.subTest(policy=policy):
                self.failure("invalid_config", document({"@id": "x", "v": "a"}), normalize=policy)
        self.failure("invalid_config", document({"sku": "x"}), identity_field="sku",
                     normalize={"/sku": "trim"})
        for value in [1, False, [], {}]:
            with self.subTest(value=value):
                error = self.failure("invalid_config", document({"@id": "x", "v": value}), normalize={"/v": "trim"})
                self.assertEqual(error.provenance[0]["file"], str(self.old))
        self.failure("invalid_config", document({"@id": "x", "v": []}),
                     set_valued=("/v",), normalize={"/v": "trim"})
        self.failure("invalid_config", document({"@id": "x", "v": "a"}),
                     syntaxes=("opengraph",), normalize={"/v": "trim"})


class FailureTests(FixtureCase):
    def test_broken_and_nonstandard_jsonld(self):
        for text in ['{"@id": "x",', '{"@id": "x",}', '//comment\n{"@id": "x"}',
                     '{"@id":"x", "v":NaN}', '{"@id":"x", "v":1e999}',
                     '{"@id":"x", "v":"\\ud800"}', '{"@id":"x", "v":1,"v":2}', '']:
            with self.subTest(text=text):
                error = self.failure("invalid_jsonld", document(scripts=[text]))
                self.assertEqual(error.provenance[0]["script_index"], 0)
                self.assertIsInstance(error.provenance[0]["line"], int)

    def test_nonobject_roots_never_silently_disappear(self):
        for value in [None, False, 0, "scalar", {}, [{"@id": "ok"}, None]]:
            with self.subTest(value=value):
                self.failure("unsupported_jsonld", document(scripts=[json.dumps(value)]))

    def test_duplicate_identity_with_two_sources(self):
        error = self.failure("duplicate_identity", document(scripts=[json.dumps({"@id": "../item"}), json.dumps({"@graph": [{"@id": "https://fixture.test/item"}]})]))
        self.assertEqual(len(error.provenance), 2)
        self.assertEqual([s["script_index"] for s in error.provenance], [0, 1])
        self.assertEqual(error.provenance[1]["pointer"], "/@graph/0")
        self.failure("duplicate_identity", document([{"sku": "x"}, {"sku": "x"}]), identity_field="sku")

    def test_missing_ambiguous_identities(self):
        for identity in [None, "", "  ", ["x"], 12]:
            self.failure("missing_identity", document({"@id": identity, "@type": "Product"}))
        self.failure("missing_identity", document({"@type": "Product"}))
        self.failure("invalid_identity", document({"@id": "bad id"}))
        self.failure("invalid_identity", document({"@id": "https://[broken"}))

    def test_unsupported_graph_shapes_rejected(self):
        for value in [{"@graph": {}}, {"@graph": [None]}, {"@graph": [{"name": "unkeyed"}]},
                      {"@id": "named", "@graph": []}, {"extra": "cannot drop", "@graph": []}]:
            code = "missing_identity" if value == {"@graph": [{"name": "unkeyed"}]} else "unsupported_jsonld"
            self.failure(code, document(value))

    def test_invalid_encoding_and_empty_html(self):
        self.old.write_bytes(b'\xff<html>')
        self.new.write_text(document(), encoding="utf-8")
        with self.assertRaisesRegex(ParityError, "invalid UTF-8 at byte 0"):
            compare_files(self.old, self.new, base_url=BASE)
        self.failure("input_encoding", '\x00<html>')
        self.failure("invalid_html", '')

    def test_missing_file_and_directory(self):
        for path in [self.directory / "missing", self.directory]:
            with self.assertRaises(ParityError) as raised:
                compare_files(path, path, base_url=BASE)
            self.assertEqual(raised.exception.code, "input_io")

    def test_invalid_base_urls(self):
        for url in ["", "relative", "file:///tmp/x", "ftp://fixture.test", "https://", "https://x:99999", "https://x:",
                    "https://user:pass@fixture.test", "https://x/#fragment", "https://x/#", "https://bad host/", "https://[bad", "https://x\\y/",
                    "https://-bad.example/", "https://a..b/", "https://bad%20host/"]:
            with self.subTest(url=url), self.assertRaises(ParityError) as raised:
                compare_files(self.old, self.new, base_url=url)
            self.assertEqual(raised.exception.code, "invalid_base_url")

    def test_invalid_syntax_and_pointer_configuration(self):
        for syntaxes in [("rdfa",), (), ("json-ld", "json-ld")]:
            with self.assertRaises(ParityError) as raised:
                compare_files(self.old, self.new, base_url=BASE, syntaxes=syntaxes)
            self.assertEqual(raised.exception.code, "unsupported_syntax")
        for paths in [("tags",), ("/bad~2",), ("/missing",), ("/@id",)]:
            self.failure("invalid_config", document({"@id": "x"}), set_valued=paths)

    def test_extraction_errors_and_missing_output_do_not_become_equality(self):
        self.old.write_text(document({"@id": "x"}), encoding="utf-8")
        self.new.write_text(document({"@id": "x"}), encoding="utf-8")
        for result in [None, {}, {"json-ld": [], "opengraph": []},
                       {"json-ld": [{"@id": "x"}], "opengraph": [{"namespace": {}, "properties": ["bad"]}]}]:
            with patch("qawrench.structured_data.core.extruct.extract", return_value=result):
                with self.assertRaises(ParityError) as raised:
                    compare_files(self.old, self.new, base_url=BASE)
                self.assertEqual(raised.exception.code, "extraction_failed")
        with patch("qawrench.structured_data.core.extruct.extract", side_effect=ValueError("upstream error")):
            with self.assertRaisesRegex(ParityError, "upstream error"):
                compare_files(self.old, self.new, base_url=BASE)

    def test_excessive_json_nesting_is_actionable(self):
        nested = '{"@id":"x","v":' + '[' * 1500 + '1' + ']' * 1500 + '}'
        self.failure("invalid_jsonld", document(scripts=[nested]))


class CliTests(FixtureCase):
    def run_cli(self, *extra):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main([str(self.old), str(self.new), "--base-url", BASE, *extra])
        return code, output.getvalue(), errors.getvalue()

    def setUp(self):
        super().setUp()
        self.old.write_text(document({"@id": "x", "price": "1"}), encoding="utf-8")
        self.new.write_text(document({"@id": "x", "price": "2"}), encoding="utf-8")

    def test_stdout_json_and_exit_policy(self):
        code, output, errors = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        self.assertEqual(json.loads(output)["status"], "different")
        self.assertEqual(self.run_cli("--fail-on-diff")[0], 1)
        self.new.write_bytes(self.old.read_bytes())
        self.assertEqual(self.run_cli("--fail-on-diff")[0], 0)

    def test_normalization_cli_and_configuration_errors(self):
        self.old.write_text(document({"@id": "x", "name": "  Tea\n mug ", "brand": "Straße"}), encoding="utf-8")
        self.new.write_text(document({"@id": "x", "name": "Tea mug", "brand": "STRASSE"}), encoding="utf-8")
        code, output, errors = self.run_cli("--normalize", "/name", "collapse-whitespace",
                                            "--normalize", "/brand", "casefold", "--fail-on-diff")
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)["status"], "equal")
        self.assertEqual(json.loads(output)["policy"]["normalize"]["/brand"], "casefold")
        for options in [("--normalize", "/name", "unknown"),
                        ("--normalize", "/missing", "trim"),
                        ("--normalize", "/name", "trim", "--normalize", "/name", "casefold")]:
            with self.subTest(options=options):
                code, output, _ = self.run_cli(*options)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(output)["errors"][0]["code"], "invalid_config")

    def test_set_valued_list_cli_returns_error_report(self):
        self.old.write_text(document({"@id": "x", "steps": {"@list": ["b", "a"]}}), encoding="utf-8")
        self.new.write_text(document({"@id": "x", "steps": {"@list": ["a", "b"]}}), encoding="utf-8")
        code, output, _ = self.run_cli("--set-valued", "/steps/@list")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["errors"][0]["code"], "invalid_config")

    def test_json_error_report_in_output_file(self):
        self.old.write_text(document(scripts=['{"broken":']), encoding="utf-8")
        out = self.directory / "error.json"
        code, stdout, stderr = self.run_cli("--out", str(out))
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("invalid_jsonld", stderr)
        report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["errors"][0]["code"], "invalid_jsonld")

    def test_unsupported_rdfa_error(self):
        code, output, _ = self.run_cli("--syntax", "rdfa")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["errors"][0]["code"], "unsupported_syntax")

    def test_markdown_report_and_safe_untrusted_values(self):
        self.new.write_text(document({"@id": "x", "price": "<img src=x>|`\nvalue"}), encoding="utf-8")
        out = self.directory / "report.md"
        code, _, _ = self.run_cli("--format", "markdown", "--out", str(out))
        self.assertEqual(code, 0)
        text = out.read_text(encoding="utf-8")
        self.assertIn("/price", text)
        self.assertIn("&lt;img", text)
        self.assertIn("&#124;", text)
        self.assertNotIn("<img", text)
        self.assertIn("Change provenance", text)
        self.assertIn("invalid_jsonld", markdown_report({"status": "error", "errors": [{"code": "invalid_jsonld", "message": "bad", "provenance": []}]}))

    def test_output_cannot_overwrite_inputs_including_symlinks(self):
        original = self.old.read_bytes()
        alias = self.directory / "alias"
        alias.symlink_to(self.old)
        for out in [self.old, alias]:
            code, stdout, _ = self.run_cli("--out", str(out))
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(stdout)["errors"][0]["code"], "output_conflict")
            self.assertEqual(self.old.read_bytes(), original)

    def test_output_write_failure_and_atomic_preservation(self):
        code, _, errors = self.run_cli("--out", str(self.directory / "absent/report.json"))
        self.assertEqual(code, 2)
        self.assertIn("output_io", errors)
        report = self.directory / "report.json"
        report.write_text("previous", encoding="utf-8")
        with patch("qawrench.structured_data.cli.os.replace", side_effect=OSError("synthetic write failure")):
            self.assertEqual(self.run_cli("--out", str(report))[0], 2)
        self.assertEqual(report.read_text(encoding="utf-8"), "previous")
        self.assertEqual(list(self.directory.glob(".parity-*")), [])

    def test_output_symlink_loop_is_actionable(self):
        loop = self.directory / "loop"
        loop.symlink_to(loop)
        code, output, errors = self.run_cli("--out", str(loop))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["errors"][0]["code"], "output_io")
        self.assertIn("output_io", errors)

    def test_actual_module_subprocess(self):
        process = subprocess.run([sys.executable, "-m", "qawrench.structured_data", str(self.old), str(self.new),
                                  "--base-url", BASE, "--fail-on-diff"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(process.returncode, 1, process.stderr)
        self.assertEqual(json.loads(process.stdout)["changes"][0]["path"], "/price")


if __name__ == "__main__":
    unittest.main()
