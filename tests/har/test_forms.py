"""Explicit form materialization: synthetic HAR 1.2, no inferred wire bytes."""
import base64
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from examples.har.fixtures import RULES, entry, har
import qawrench.har as doctor

ROOT = Path(__file__).resolve().parents[2]
MIME = "application/x-www-form-urlencoded"
PARAMS = [{"name": "tag", "value": "a b"}, {"name": "tag", "value": "é+"},
          {"name": "empty", "value": "", "comment": "keep"}]
TEXT = "tag=a+b&tag=%C3%A9%2B&empty="


def form_entry(params=PARAMS):
    item = entry("https://stage.example/form?cacheBust=1", "POST")
    item["request"]["bodySize"] = -1
    item["request"]["postData"] = {"mimeType": MIME, "params": copy.deepcopy(params)}
    return item


class FormTests(unittest.TestCase):
    def test_explicit_embedding_is_pure_ordered_and_integrates_with_matching(self):
        source = har([form_entry()])
        original = copy.deepcopy(source)
        # Ordinary rewriting never assumes the original serializer's spelling.
        with self.assertRaisesRegex(doctor.DoctorError, "embedded text"):
            doctor.build_plan(source, RULES)
        result = doctor.embed_form_params(source)
        expected = copy.deepcopy(source)
        expected["log"]["entries"][0]["request"]["postData"]["text"] = TEXT
        self.assertEqual(result, expected)
        self.assertEqual(source, original)
        self.assertEqual(doctor.embed_form_params(result), result)
        self.assertEqual(doctor.request_key(result["log"]["entries"][0]["request"])[2], TEXT.encode("ascii"))
        wanted = {"schemaVersion": 1, "requests": [{"id": "form", "url": "http://fixture.test/form",
                  "method": "POST", "postData": {"mimeType": MIME, "text": TEXT}}]}
        rewritten, plan = doctor.rewrite_har(result, RULES, inventory=wanted)
        self.assertTrue(plan["requestPreflight"]["passed"])
        self.assertEqual(rewritten["log"]["entries"][0]["request"]["postData"]["text"], TEXT)
        duplicate = copy.deepcopy(result["log"]["entries"][0])
        duplicate["request"]["postData"] = {"mimeType": MIME, "encoding": "base64",
                                           "text": base64.b64encode(TEXT.encode()).decode()}
        duplicate["response"]["status"] = 201
        collision = har([result["log"]["entries"][0], duplicate])
        self.assertFalse(doctor.build_plan(collision, RULES)["safeToApply"])

    def test_serializer_handles_utf8_reserved_bytes_empty_names_and_empty_forms(self):
        pairs = [{"name": "*~-._!", "value": "&=%+ /\r\n\x00💧"}, {"name": "", "value": ""}]
        source = har([form_entry(pairs), form_entry([])])
        source["log"]["entries"][0]["request"]["postData"]["mimeType"] = MIME.upper() + '; charset="UTF-8"'
        result = doctor.embed_form_params(source)
        self.assertEqual([e["request"]["postData"]["text"] for e in result["log"]["entries"]],
                         ["*%7E-._%21=%26%3D%25%2B+%2F%0D%0A%00%F0%9F%92%A7&=", ""])

    def test_existing_text_base64_multipart_and_response_bytes_are_authoritative(self):
        raw = form_entry()
        raw["request"]["postData"]["text"] = "tag=a%20b&tag=%c3%a9%2b&empty="
        binary = entry("https://stage.example/binary", "POST", "AP9B", binary=True)
        binary["request"]["postData"].update(mimeType="application/octet-stream", encoding="base64")
        multipart = entry("https://stage.example/upload", "POST",
                          '--b\r\nContent-Disposition: form-data; name="field"\r\n\r\nraw\r\n--b--\r\n')
        multipart["request"]["postData"]["mimeType"] = "multipart/form-data; boundary=b"
        source = har([form_entry(), raw, binary, multipart])
        original = copy.deepcopy(source)
        result = doctor.embed_form_params(source)
        self.assertEqual(result["log"]["entries"][1:], original["log"]["entries"][1:])
        self.assertEqual(source, original)
        self.assertEqual(doctor.request_key(result["log"]["entries"][2]["request"])[2], b"\x00\xffA")

    def test_unsupported_or_lossy_forms_fail_without_mutating_any_entry(self):
        cases = [({"mimeType": "multipart/form-data; boundary=b"}, "mimeType"),
                 ({"mimeType": MIME + "; charset=iso-8859-1"}, "mimeType"),
                 ({"mimeType": MIME + "; boundary=b"}, "mimeType"),
                 ({"encoding": "gzip"}, "encoding"), ({"encoding": "base64"}, "encoding"),
                 ({"encoding": None}, "encoding"), ({"_file": "secret"}, "attached"),
                 ({"_sha1": "secret"}, "attached"), ({"_encoding": "gzip"}, "unknown fields"),
                 ({"params": None}, "params"), ({"params": [{"name": "a"}]}, "value"),
                 ({"params": [{"name": "a", "value": None}]}, "value"),
                 ({"params": [{"name": "a", "value": "", "fileName": ""}]}, "fileName"),
                 ({"params": [{"name": "a", "value": "", "contentType": "text/plain"}]}, "contentType"),
                 ({"params": [{"name": "a", "value": "", "encoding": "base64"}]}, "encoding"),
                 ({"params": [{"name": "\ud800", "value": "x"}]}, "surrogate")]
        for fields, message in cases:
            source = har([form_entry(), form_entry()])
            source["log"]["entries"][1]["request"]["postData"].update(fields)
            original = copy.deepcopy(source)
            with self.subTest(fields=repr(fields)), self.assertRaisesRegex(doctor.DoctorError, message):
                doctor.embed_form_params(source)
            self.assertEqual(source, original)
        # Whole-HAR validation also precedes publication.
        source = har([form_entry()])
        source["log"]["entries"][0]["response"]["content"]["_file"] = "secret"
        with self.assertRaisesRegex(doctor.DoctorError, "attached"):
            doctor.embed_form_params(source)

    def test_known_size_and_headers_must_agree_with_synthesized_bytes(self):
        good = form_entry()
        good["request"].update(bodySize=len(TEXT), headers=[
            {"name": "Content-Type", "value": MIME + "; charset=utf-8"},
            {"name": "Content-Length", "value": str(len(TEXT))},
            {"name": "Content-Encoding", "value": "identity"}])
        self.assertEqual(doctor.embed_form_params(har([good]))["log"]["entries"][0]["request"]["bodySize"], len(TEXT))
        for fields, message in [({"bodySize": 0}, "bodySize"), ({"bodySize": len(TEXT) + 1}, "bodySize"),
                                ({"headers": [{"name": "Content-Encoding", "value": "gzip"}]}, "Content-Encoding"),
                                ({"headers": [{"name": "Content-Type", "value": MIME + "; charset=latin1"}]}, "Content-Type"),
                                ({"headers": [{"name": "Content-Length", "value": "1"}]}, "Content-Length"),
                                ({"headers": [{"name": "Content-Length", "value": "9" * 5000}]}, "Content-Length"),
                                ({"headers": [{"name": "Content-Length", "value": "invalid"}]}, "Content-Length")]:
            item = form_entry()
            item["request"].update(fields)
            with self.subTest(fields=fields), self.assertRaisesRegex(doctor.DoctorError, message):
                doctor.embed_form_params(har([item]))

    def test_cli_embeds_or_refuses_whole_file_atomically_and_never_clobbers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "forms.har"
            source.write_text(json.dumps(har([form_entry()])))
            original = source.read_bytes()

            def run(out):
                return subprocess.run([sys.executable, "-m", "qawrench.har", "embed-forms",
                                       str(source), "--out", str(out)], cwd=ROOT,
                                      capture_output=True, text=True, timeout=20)

            output = root / "embedded.har"
            result = run(output)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["embeddedRequests"], 1)
            self.assertEqual(json.loads(output.read_text())["log"]["entries"][0]["request"]["postData"]["text"], TEXT)
            output_bytes = output.read_bytes()
            self.assertEqual(run(output).returncode, 2)
            self.assertEqual(output.read_bytes(), output_bytes)
            self.assertEqual(run(source).returncode, 2)
            self.assertEqual(source.read_bytes(), original)
            bad = har([form_entry(), form_entry()])
            bad["log"]["entries"][1]["request"]["postData"]["encoding"] = "gzip"
            source.write_text(json.dumps(bad))
            bad_bytes = source.read_bytes()
            blocked = root / "blocked.har"
            result = run(blocked)
            self.assertEqual(result.returncode, 2)
            self.assertIn("encoding", result.stderr)
            self.assertFalse(blocked.exists())
            self.assertEqual(source.read_bytes(), bad_bytes)
            self.assertFalse(list(root.glob(".*.tmp")))


if __name__ == "__main__":
    unittest.main()
