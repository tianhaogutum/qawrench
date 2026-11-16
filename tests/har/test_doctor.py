"""Substantive synthetic/offline API, CLI, persistence and failure-path tests."""
import base64
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from examples.har.fixtures import RULES, entry, example_har, har
from qawrench.har import DoctorError, build_plan, load_json, request_key, rewrite_har, validate_har, validate_rules
from qawrench.har.core import atomic_write_json

ROOT = Path(__file__).resolve().parents[2]


class RewriteTests(unittest.TestCase):
    def test_get_post_binary_roundtrip_preserves_untouched_fields(self):
        source = example_har()
        original = copy.deepcopy(source)
        rules_copy = copy.deepcopy(RULES)
        result, plan = rewrite_har(source, RULES)
        self.assertEqual(source, original)
        self.assertEqual(RULES, rules_copy)
        self.assertEqual(plan["summary"], {"entries": 4, "changedEntries": 3, "collisionGroups": 0, "conflictingGroups": 0})
        self.assertEqual(result["log"]["entries"][0]["request"]["url"],
                         "http://fixture.test/items?tag=red&tag=blue&empty=&q=a%20b")
        for old, new in zip(source["log"]["entries"], result["log"]["entries"]):
            self.assertEqual(old["response"], new["response"])
            restored = copy.deepcopy(new)
            restored["request"]["url"] = old["request"]["url"]
            restored["request"]["queryString"] = old["request"]["queryString"]
            self.assertEqual(restored, old)
        self.assertEqual(result["log"]["entries"][1]["request"]["postData"]["text"], '{"id":2,"name":"é"}')
        self.assertEqual(base64.b64decode(result["log"]["entries"][2]["response"]["content"]["text"]), b"\x00\xff\r\n\x80")
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_query_tokens_not_reserialized(self):
        url = "https://stage.example/p?x=a+b&cache%42ust=1&x=a%20b&cacheBust=2&flag&&X=%2f&cacheBust=&=v#fragment"
        source = har([entry(url)])
        result, plan = rewrite_har(source, RULES)
        self.assertEqual(result["log"]["entries"][0]["request"]["url"],
                         "http://fixture.test/p?x=a+b&x=a%20b&flag&&X=%2f&=v#fragment")
        reasons = plan["entries"][0]["reasons"]
        self.assertEqual([p["tokenIndex"] for p in reasons[1]["tokens"]], [1, 3, 7])
        self.assertEqual([p["name"] for p in result["log"]["entries"][0]["request"]["queryString"]], ["x", "x", "flag", "X", ""])

    def test_origin_is_literal_and_suffix_hosts_ports_paths_untouched(self):
        urls = ["https://stage.example.evil.test/a", "https://sub.stage.example/a", "https://stage.example:443/a",
                "http://stage.example/a", "https://STAGE.example/a", "https://other.test/https://stage.example"]
        result, plan = rewrite_har(har([entry(url) for url in urls]), RULES)
        self.assertEqual([e["request"]["url"] for e in result["log"]["entries"]], urls)
        self.assertEqual(plan["summary"]["changedEntries"], 0)
        ipv6, _ = rewrite_har(har([entry("http://[::1]:8000/a")]), {"origins": {"http://[::1]:8000": "http://fixture.test:8001"}})
        self.assertEqual(ipv6["log"]["entries"][0]["request"]["url"], "http://fixture.test:8001/a")

    def test_empty_query_all_dropped_and_repeated_apply_idempotent(self):
        source = har([entry("https://stage.example/a?cacheBust=1"), entry("https://stage.example/b?"),
                      entry("https://stage.example/c?cacheBust=1&"), entry("https://stage.example/d")])
        result, _ = rewrite_har(source, RULES)
        self.assertEqual([e["request"]["url"] for e in result["log"]["entries"]],
                         ["http://fixture.test/a", "http://fixture.test/b?", "http://fixture.test/c?", "http://fixture.test/d"])
        second, plan = rewrite_har(result, RULES)
        self.assertEqual(second, result)
        self.assertEqual(plan["summary"]["changedEntries"], 0)
        self.assertEqual(rewrite_har(source, {})[0], source)
        self.assertEqual(build_plan(source, RULES), build_plan(source, RULES))
        self.assertEqual(build_plan(har([]), {})["summary"]["entries"], 0)

    def test_duplicate_collision_safe_but_entries_not_merged(self):
        source = har([entry("https://stage.example/a?cacheBust=1"), entry("http://fixture.test/a")])
        result, plan = rewrite_har(source, RULES)
        group = plan["collisionGroups"][0]
        self.assertFalse(group["conflict"])
        self.assertEqual(group["entryIndexes"], [0, 1])
        self.assertEqual(group["key"], {"url": "http://fixture.test/a", "method": "GET", "bodyBase64": ""})
        self.assertEqual(len(result["log"]["entries"]), 2)

    def test_each_response_difference_blocks_even_preexisting_collisions(self):
        for field, value in (("status", 201), ("headers", [{"name": "Vary", "value": "Accept"}]),
                             ("content", {"mimeType": "text/plain", "text": "other", "size": 5}),
                             ("_custom", {"extra": True}), ("redirectURL", "https://other.test/")):
            with self.subTest(field=field):
                source = har([entry("https://stage.example/a?cacheBust=1"), entry("http://fixture.test/a")])
                source["log"]["entries"][1]["response"][field] = value
                plan = build_plan(source, RULES)
                self.assertFalse(plan["safeToApply"])
                self.assertEqual(plan["collisionGroups"][0]["differingResponseFields"], [field])
                with self.assertRaisesRegex(DoctorError, "conflicting rewritten request keys"):
                    rewrite_har(source, RULES)
        source = har([entry("http://fixture.test/a"), entry("http://fixture.test/a", response_text="different")])
        self.assertFalse(build_plan(source, {})["safeToApply"])

    def test_method_and_exact_body_define_distinct_groups(self):
        source = har([entry("https://stage.example/a?cacheBust=1", "POST", '{"a":1}'),
                      entry("https://stage.example/a?cacheBust=2", "POST", '{ "a":1}', response_text="different"),
                      entry("https://stage.example/a", "GET", response_text="get")])
        self.assertEqual(build_plan(source, RULES)["summary"]["collisionGroups"], 0)
        self.assertNotEqual(request_key(source["log"]["entries"][0]["request"]), request_key(source["log"]["entries"][1]["request"]))
        # Base64 and text representations of identical request bytes group together.
        source["log"]["entries"][1] = copy.deepcopy(source["log"]["entries"][0])
        source["log"]["entries"][1]["request"]["postData"].update(text="eyJhIjoxfQ==", encoding="base64")
        self.assertEqual(build_plan(source, RULES)["summary"]["collisionGroups"], 1)

    def test_minimal_metadata_and_empty_embedded_response(self):
        minimal = {"request": {"method": "GET", "url": "http://fixture.test/", "headers": []},
                   "response": {"status": 204, "headers": [], "content": {"size": 0, "mimeType": "text/plain"}}}
        source = har([minimal])
        self.assertEqual(rewrite_har(source, {})[0], source)


class ValidationTests(unittest.TestCase):
    def assert_bad_mutation(self, path, value, message):
        source = example_har()
        current = source
        for part in path[:-1]:
            current = current[part]
        current[path[-1]] = value
        with self.assertRaisesRegex(DoctorError, message):
            build_plan(source, RULES)

    def test_malformed_har_structure_and_types(self):
        cases = [(["log", "version"], "1.3", "only HAR 1.2"), (["log", "entries"], {}, "array"),
                 (["log", "creator"], None, "object"), (["log", "entries", 0, "request"], None, "object"),
                 (["log", "entries", 0, "response"], {}, "status"),
                 (["log", "entries", 0, "request", "headers"], [{"name": "X"}], "value"),
                 (["log", "entries", 0, "request", "queryString"], "x=1", "array"),
                 (["log", "entries", 0, "request", "method"], "GET\n", "method"),
                 (["log", "entries", 0, "request", "bodySize"], "bad", "bodySize"),
                 (["log", "entries", 0, "response", "status"], True, "status"),
                 (["log", "entries", 0, "response", "status"], 600, "status"),
                 (["log", "entries", 0, "response", "content"], None, "content"),
                 (["log", "entries", 0, "response", "content", "size"], float("nan"), "finite"),
                 (["log", "entries", 0, "response", "content", "text"], "\ud800", "surrogate")]
        for path, value, message in cases:
            with self.subTest(path=path, value=repr(value)):
                self.assert_bad_mutation(path, value, message)

    def test_invalid_urls(self):
        for url in ("/relative", "ftp://fixture.test/a", "https://a:99999/", "https://user:pass@a/",
                    "https://a/a\nb", "https://a/%ZZ", "https://a/?%ff=1", "https://a\\b/c", "https://a:/", "https://[::1]evil.test/", "https://a:80:90/"):
            with self.subTest(url=url), self.assertRaises(DoctorError):
                validate_har(har([entry("http://fixture.test/") | {"request": {"url": url, "method": "GET", "headers": []}}]))

    def test_attached_omitted_params_only_and_bad_encoded_bodies(self):
        for part in ("request", "response"):
            for marker in ("_file", "_sha1"):
                source = example_har()
                payload = source["log"]["entries"][1][part]["postData" if part == "request" else "content"]
                payload[marker] = "payload.bin"
                with self.subTest(part=part, marker=marker), self.assertRaisesRegex(DoctorError, "attached"):
                    validate_har(source)
        for payload in ({"mimeType": "application/json", "params": []},
                        {"mimeType": "application/json", "text": "!!", "encoding": "base64"},
                        {"mimeType": "application/json", "text": "abc", "encoding": "gzip"},
                        {"mimeType": "application/json", "text": "abc", "encoding": None}):
            with self.subTest(payload=payload):
                self.assert_bad_mutation(["log", "entries", 1, "request", "postData"], payload, "postData")
        self.assert_bad_mutation(["log", "entries", 0, "response", "content"], {"size": 50, "mimeType": "text/plain"}, "embedded text")
        self.assert_bad_mutation(["log", "entries", 0, "request", "bodySize"], 10, "postData")

    def test_malformed_rules_unknown_fields_and_non_idempotent_mappings(self):
        cases = [[], {"regex": "x"}, {"origins": []}, {"origins": {"https://a/": "https://b"}},
                 {"origins": {"https://a": "https://b/path"}}, {"origins": {"https://a": "https://b", "https://b": "https://c"}},
                 {"origins": {"https://a": "https://b", "https://b": "https://a"}},
                 {"dropQueryKeys": "cacheBust"}, {"dropQueryKeys": [""]}, {"dropQueryKeys": [1]},
                 {"dropQueryKeys": ["a", "a"]}]
        for rules in cases:
            with self.subTest(rules=rules), self.assertRaises(DoctorError):
                validate_rules(rules)
        validate_rules({"origins": {"https://a": "https://b", "https://b": "https://b"}})


class FileAndCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "input.har"
        self.rules = self.root / "rules.json"
        self.input.write_text(json.dumps(example_har()), encoding="utf-8")
        self.rules.write_text(json.dumps(RULES), encoding="utf-8")
        self.original = self.input.read_bytes()

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "qawrench.har", *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True, timeout=20)

    def command(self, command, output):
        return self.cli(command, self.input, "--rules", self.rules, "--out", output)

    def test_cli_plan_writes_only_plan_and_apply_matches_library(self):
        output = self.root / "plan.json"
        run = self.command("plan", output)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual({p.name for p in self.root.iterdir()}, {"input.har", "rules.json", "plan.json"})
        self.assertNotIn("log", json.loads(output.read_text()))
        self.assertTrue(json.loads(run.stdout)["safeToApply"])
        applied = self.root / "output.har"
        run = self.command("apply", applied)
        self.assertEqual(run.returncode, 0, run.stderr)
        expected, _ = rewrite_har(load_json(self.input), RULES)
        self.assertEqual(json.loads(applied.read_text()), expected)
        self.assertEqual(self.input.read_bytes(), self.original)
        repeat = self.root / "repeat.har"
        self.assertEqual(self.command("apply", repeat).returncode, 0)
        self.assertEqual(applied.read_bytes(), repeat.read_bytes())

    def test_conflict_plan_written_apply_blocked_original_bytes_unchanged(self):
        source = har([entry("https://stage.example/a?cacheBust=1"), entry("http://fixture.test/a", response_text="other")])
        self.input.write_text(json.dumps(source))
        original = self.input.read_bytes()
        run = self.command("plan", self.root / "plan.json")
        self.assertEqual(run.returncode, 2)
        self.assertFalse(json.loads((self.root / "plan.json").read_text())["safeToApply"])
        run = self.command("apply", self.root / "output.har")
        self.assertEqual(run.returncode, 2)
        self.assertIn("conflicting", run.stderr)
        self.assertFalse((self.root / "output.har").exists())
        self.assertEqual(self.input.read_bytes(), original)

    def test_same_path_existing_hardlink_symlink_and_missing_parent_rejected(self):
        hardlink = self.root / "hardlink.har"
        os.link(self.input, hardlink)
        symlink = self.root / "symlink.har"
        symlink.symlink_to(self.input)
        dangling = self.root / "dangling.har"
        dangling.symlink_to(self.root / "not-here.har")
        for output in (self.input, self.rules, hardlink, symlink, dangling, self.root / "absent" / "out.har"):
            with self.subTest(output=output):
                run = self.command("apply", output)
                self.assertEqual(run.returncode, 2, run.stdout)
                self.assertNotIn("Traceback", run.stderr)
        self.assertEqual(self.input.read_bytes(), self.original)
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_atomic_write_failure_and_racing_writer_leave_no_partial_output(self):
        output = self.root / "out.json"
        with patch("qawrench.har.core.os.fsync", side_effect=OSError("simulated disk failure")):
            with self.assertRaisesRegex(DoctorError, "simulated disk failure"):
                atomic_write_json({"complete": True}, output)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".*.tmp")), [])
        def racing_link(source, target):
            Path(target).write_text("racer's complete content")
            raise FileExistsError("racing writer won")
        with patch("qawrench.har.core.os.link", side_effect=racing_link):
            with self.assertRaisesRegex(DoctorError, "racing writer won"):
                atomic_write_json({"complete": True}, output)
        self.assertEqual(output.read_text(), "racer's complete content")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_bad_json_duplicate_keys_nonexistent_and_invalid_rules_actionable(self):
        for content in ("{\"tooLarge\": " + "9" * 5000 + "}", "{", '{"log": {}, "log": {}}', '{"a": NaN}', '{"a": 1e999}', '"\\ud800"'):
            self.input.write_text(content)
            with self.subTest(content=content):
                run = self.command("plan", self.root / "out.json")
                self.assertEqual(run.returncode, 2)
                self.assertNotIn("Traceback", run.stderr)
                self.assertFalse((self.root / "out.json").exists())
        self.input.unlink()
        run = self.command("apply", self.root / "out.har")
        self.assertEqual(run.returncode, 2)
        self.assertIn("cannot read JSON", run.stderr)
        self.input.write_bytes(self.original)
        self.rules.write_text('{"dropQueryKeys":"all"}')
        run = self.command("plan", self.root / "out.json")
        self.assertEqual(run.returncode, 2)
        self.assertIn("dropQueryKeys", run.stderr)

    def test_cli_never_executes_fixture_or_rules_content(self):
        marker = self.root / "MUST_NOT_EXIST"
        source = example_har()
        source["log"]["entries"][0]["response"]["content"]["text"] = f"$(touch {marker})"
        self.input.write_text(json.dumps(source))
        run = self.command("plan", self.root / "plan.json")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse(marker.exists())
        source["log"]["entries"][0]["response"]["content"]["_file"] = "missing.bin"
        self.input.write_text(json.dumps(source))
        run = self.command("apply", self.root / "attached.har")
        self.assertEqual(run.returncode, 2)
        self.assertIn("attached", run.stderr)
        self.assertFalse((self.root / "attached.har").exists())
        self.rules.write_text(json.dumps({"script": f"touch {marker}"}))
        self.assertEqual(self.command("apply", self.root / "out.har").returncode, 2)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
