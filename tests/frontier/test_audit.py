"""All records are synthetic and all subprocesses are local/offline."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from w3lib.url import canonicalize_url, parse_qsl_to_bytes

from qawrench.frontier import AuditError, Policy, audit, load_requests, proposed_url
from qawrench.frontier.cli import atomic_write, main
from qawrench.frontier.core import parse_request, strict_json, validate_url
from examples.frontier.experiment import run, seeded_records

ROOT = Path(__file__).resolve().parents[2]


def requests(*records):
    return [parse_request(r, i, f"line {i}", "jsonl") for i, r in enumerate(records, 1)]


class PolicyTests(unittest.TestCase):
    def test_default_is_exact_for_adversarial_query_and_delimiters(self):
        for suffix in ("?b=2&a=1&a=1", "?flag&blank=&=x&&a=+&a=%20", "?q=%ff&q=%FE",
                       "?#", "#", "?", "/%7e?a=%2f", "?x=a;b&x=a%3Bb"):
            url = "HTTPS://BÜCHER.test" + suffix
            self.assertEqual(proposed_url(validate_url(url), Policy()), url)

    def test_tracking_removes_only_named_decoded_keys(self):
        policy = Policy(remove_query_keys=("utm_source",))
        url = "https://example.test/?a=%ff&utm_source=one&a=&%75tm_source=two&UTM_source=z&x=+&&flag#keep"
        self.assertEqual(proposed_url(url, policy), "https://example.test/?a=%ff&a=&UTM_source=z&x=+&&flag#keep")
        self.assertEqual(proposed_url("https://example.test/?utm_source=1#x", policy), "https://example.test/#x")

    def test_fragment_policy_and_empty_markers(self):
        self.assertEqual(proposed_url("https://example.test/?#", Policy(fragment="drop")), "https://example.test/?")
        self.assertEqual(proposed_url("https://example.test/?q=%23x#x", Policy(fragment="drop")), "https://example.test/?q=%23x")

    def test_actual_w3lib_query_integration_is_scoped(self):
        url = "HTTPS://BÜCHER.test/%7e?b=2&a=%ff&a=+&flag&a=%20#keep"
        query = url.split("?", 1)[1].split("#")[0]
        expected_query = canonicalize_url("https://query.invalid/?" + query,
                                         keep_blank_values=True, keep_fragments=True).split("?", 1)[1]
        result = proposed_url(url, Policy(w3lib_sort_query=True))
        self.assertEqual(result, "HTTPS://BÜCHER.test/%7e?" + expected_query + "#keep")
        self.assertEqual(sorted(parse_qsl_to_bytes(query, True)), parse_qsl_to_bytes(expected_query, True))
        self.assertIn("a=%FF", result)
        self.assertIn("flag=", result)

    def test_w3lib_preserves_repeated_pair_multiplicity(self):
        policy = Policy(w3lib_sort_query=True)
        self.assertEqual(proposed_url("https://example.test/?k=b&k=a&k=a&x=", policy),
                         "https://example.test/?k=a&k=a&k=b&x=")
        self.assertNotEqual(proposed_url("https://example.test/?k=a&k=a", policy),
                            proposed_url("https://example.test/?k=a", policy))

    def test_idna_separate_explicit_python_idna_2003_policy(self):
        url = "HTTPS://Bücher.test:00443/%7e?b=2&a=1#x"
        self.assertEqual(proposed_url(url, Policy(idna="ascii")),
                         "HTTPS://xn--bcher-kva.test:00443/%7e?b=2&a=1#x")
        self.assertEqual(proposed_url("http://[2001:db8::1]:80/?x=1", Policy(idna="ascii")),
                         "http://[2001:db8::1]:80/?x=1")

    def test_policy_rejects_unknown_and_invalid_fields(self):
        bad = [{"unknown": 1}, {"version": 2}, {"version": True}, {"fragment": "ignore"},
               {"idna": "uts46"}, {"w3lib_sort_query": 1}, {"allow_ambiguous_merges": "yes"},
               {"remove_query_keys": "utm"}, {"remove_query_keys": [""]},
               {"remove_query_keys": [None]}, {"remove_query_keys": ["a", "a"]},
               {"remove_query_keys": ["a+b"]}, [], None]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(AuditError):
                Policy.from_dict(data)


class AuditTests(unittest.TestCase):
    def test_methods_and_exact_bodies_stay_separate_and_warn(self):
        records = requests(*[{"url": "https://example.test/?utm=x", "method": method, "body": body}
                             for method, body in (("GET", ""), ("POST", ""), ("POST", "x"), ("POST", "x "), ("POST", "X"))])
        frontier, report = audit(records, Policy(remove_query_keys=("utm",)))
        self.assertEqual(len(frontier), 5)
        self.assertEqual(report["status"], "ok")  # no policy-created URL collision here
        self.assertEqual(len(report["protected_url_groups"]), 1)
        self.assertTrue(report["warnings"])

    def test_created_cross_body_url_collisions_fail_without_permission(self):
        records = requests({"url": "https://example.test/?utm=a", "body": "x"},
                           {"url": "https://example.test/?utm=b", "body": "y"})
        frontier, report = audit(records, Policy(remove_query_keys=("utm",)))
        self.assertEqual(len(frontier), 2)
        self.assertEqual(report["status"], "rejected")
        self.assertIn("different methods/bodies", report["errors"][0])

    def test_every_collision_group_and_changed_witness_in_order(self):
        records = requests({"url": "https://example.test/?utm=a", "source": "one"},
                           {"url": "https://example.test/?utm=b", "source": "two"},
                           {"url": "https://example.test/?utm=a", "source": "three"},
                           {"url": "https://other.test/"}, {"url": "https://other.test/"})
        policy = Policy(remove_query_keys=("utm",), allow_ambiguous_merges=True)
        frontier, report = audit(records, policy)
        self.assertEqual([r["url"] for r in frontier], [records[0].url, records[3].url])
        self.assertEqual(len(report["collision_groups"]), 2)
        self.assertEqual(len(report["exact_collision_groups"]), 2)
        self.assertEqual(len(report["changed_key_witnesses"]), 3)
        self.assertEqual([r["index"] for r in report["collision_groups"][0]["members"]], [1, 2, 3])
        self.assertEqual(report["collision_groups"][0]["distinct_exact_keys"], 2)
        self.assertEqual(report["collision_groups"][0]["members"][2]["original"]["source"], "three")
        self.assertEqual(report["counts"]["duplicates"], 3)
        self.assertEqual(report["counts"]["policy_created_collision_groups"], 1)
        self.assertEqual(audit(records, policy), (frontier, report))

    def test_hash_is_only_display_not_identity(self):
        records = requests({"url": "https://example.test/a"}, {"url": "https://example.test/b"})
        with patch("qawrench.frontier.core.fingerprint", return_value="same"):
            frontier, report = audit(records, Policy())
        self.assertEqual(len(frontier), 2)
        self.assertEqual(report["collision_groups"], [])

    def test_sorting_order_sensitive_repeated_keys_requires_permission(self):
        records = requests({"url": "https://example.test/?k=b&k=a"}, {"url": "https://example.test/?k=a&k=b"})
        self.assertEqual(audit(records, Policy())[1]["counts"]["proposed_unique"], 2)
        frontier, report = audit(records, Policy(w3lib_sort_query=True))
        self.assertEqual(len(frontier), 1)
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(len(report["collision_groups"][0]["members"]), 2)

    def test_fixture_counts_and_retained_originals(self):
        records, issues, total = load_requests(ROOT / "examples/frontier/requests.jsonl")
        policy = Policy.from_dict(strict_json((ROOT / "examples/frontier/policy.json").read_text()))
        frontier, report = audit(records, policy, issues=issues, total=total)
        self.assertEqual(report["counts"]["raw_records"], 11)
        self.assertEqual(report["counts"]["raw_url_unique"], 9)
        self.assertEqual(report["counts"]["exact_unique"], 10)
        self.assertEqual(report["counts"]["proposed_unique"], 8)
        self.assertEqual(frontier[0]["url"], records[0].url)
        self.assertIn("utm_source=one#top", frontier[0]["url"])


class InputTests(unittest.TestCase):
    def test_reject_malformed_and_unsupported_urls(self):
        for url in (None, "", "relative/path", "https:///x", "https://", "http://host:0/", "http://host:65536/",
                    "https://host:/", "https://host:no/", "http://[::1]junk/", "https://[bad]/", "http://foo..test/",
                    "https://-host.test/", "https://host/has space", "https://host/\n", "https://host/%Z0",
                    "https://host/\\x", "https://user:pass@host/", "file:///tmp/x", "ftp://host/x", "http://[::1%25eth0]/"):
            with self.subTest(url=url), self.assertRaises(AuditError):
                validate_url(url)

    def test_invalid_records_rejected(self):
        for record in ([1], "https://example.test/", {}, {"url": "https://example.test/", "body": None},
                       {"url": "https://example.test/", "body": {}}, {"url": "https://example.test/", "body": "\ud800"},
                       {"url": "https://example.test/", "method": "get"}, {"url": "https://example.test/", "method": "GET X"},
                       {"url": "https://example.test/", "method": 3}, {"url": "https://example.test/", "source": 3},
                       {"url": "https://example.test/", "headers": {}}, {"url": "https://example.test/", "metadata": None}):
            with self.subTest(record=record), self.assertRaises(AuditError):
                parse_request(record, 1, "line 1", "jsonl")

    def test_json_duplicates_nonfinite_and_overflow_are_not_silently_lost(self):
        for text in ('{"a":1,"a":2}', 'NaN', 'Infinity', '1e9999', '{bad'):
            with self.subTest(text=text), self.assertRaises(AuditError):
                strict_json(text)

    def test_jsonl_issues_and_skip_accounting(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "input.jsonl"
            path.write_text('{"url":"https://example.test/"}\nnot JSON\n{"url":"ftp://example.test/"}\n\n')
            records, issues, total = load_requests(path)
        self.assertEqual(total, 4)
        self.assertEqual([i["kind"] for i in issues], ["invalid", "unsupported", "invalid"])
        self.assertEqual(audit(records, Policy(), issues=issues, total=total)[1]["status"], "rejected")
        _, report = audit(records, Policy(), issues=issues, total=total, on_invalid="skip")
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["counts"]["invalid"], 2)
        self.assertEqual(report["counts"]["unsupported"], 1)

    def test_unicode_line_separator_inside_exact_body_is_not_record_separator(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "input.jsonl"
            path.write_text(json.dumps({"url": "https://example.test/", "body": "a\u2028b"}, ensure_ascii=False) + "\n")
            records, issues, total = load_requests(path)
        self.assertFalse(issues)
        self.assertEqual(total, 1)
        self.assertEqual(records[0].body, "a\u2028b")

    def test_crawlee_actual_documented_sources_shape(self):
        records, issues, total = load_requests(ROOT / "examples/frontier/crawlee-sources.json", "crawlee")
        self.assertEqual(total, 4)
        self.assertEqual(issues, [])
        frontier, report = audit(records, Policy())
        self.assertEqual(len(frontier), 3)
        self.assertEqual(frontier[1]["body"], "a=1&a=2")
        self.assertEqual(frontier[1]["metadata"]["userData"]["label"], "SYNTHETIC")
        self.assertEqual(report["representatives"][0]["original"], "https://example.test/page-1")
        # Frontier metadata remains ingestible in our JSONL dialect.
        self.assertEqual(len(requests(*frontier)), 3)

    def test_crawlee_no_remote_sources_or_silent_ignored_semantics(self):
        for record in ({"requestsFromUrl": "https://example.test/list.txt"},
                       {"url": "https://example.test/", "headers": {"Accept": "text/plain"}},
                       {"url": "https://example.test/", "uniqueKey": "override"},
                       {"url": "https://example.test/", "payload": {"type": "Buffer", "data": [1]}},
                       {"url": "https://example.test/", "userData": []}):
            with self.subTest(record=record), self.assertRaises(AuditError):
                parse_request(record, 1, "item 1", "crawlee")

    def test_bad_crawlee_envelope_and_unknown_format(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "input.json"
            for text in ('{"sources": []}', '[{"url":'):
                path.write_text(text)
                with self.assertRaises(AuditError):
                    load_requests(path, "crawlee")
            with self.assertRaises(AuditError):
                load_requests(path, "future")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "input.jsonl"
        self.frontier = self.root / "frontier.jsonl"
        self.report = self.root / "report.json"
        self.input.write_text('{"url":"https://example.test/?a=1"}\n')
        self.args = [str(self.input), "--frontier", str(self.frontier), "--report", str(self.report)]

    def cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = main(args)
        return status, out.getvalue(), err.getvalue()

    def test_cli_fixture_byte_determinism_and_console_entrypoint(self):
        args = [str(ROOT / "examples/frontier/requests.jsonl"), "--policy", str(ROOT / "examples/frontier/policy.json"),
                "--frontier", str(self.frontier), "--report", str(self.report)]
        command = [sys.executable, "-m", "qawrench.frontier", *args]
        subprocess.run(command, check=True, capture_output=True)
        first = self.frontier.read_bytes(), self.report.read_bytes()
        subprocess.run(command, check=True, capture_output=True)
        self.assertEqual(first, (self.frontier.read_bytes(), self.report.read_bytes()))
        self.assertTrue(json.loads(first[1])["frontier_emitted"])
        completed = subprocess.run([sys.executable, "-m", "qawrench", "frontier", str(self.input), "--dry-run"],
                                   check=True, capture_output=True)
        self.assertEqual(json.loads(completed.stdout)["counts"]["accepted"], 1)

    def test_dry_run_never_writes_or_executes_scripts(self):
        marker = self.root / "executed"
        self.input.write_text(json.dumps({"url": "https://example.test/", "body": f"touch {marker}"}) + "\n")
        with patch("qawrench.frontier.cli.atomic_write", side_effect=AssertionError("must not write")):
            code, out, _ = self.cli(self.args + ["--dry-run"])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["frontier_emitted"])
        self.assertFalse(marker.exists())
        self.assertFalse(self.report.exists())
        self.assertFalse(self.frontier.exists())

    def test_invalid_input_default_report_no_frontier_and_explicit_skip(self):
        self.input.write_text(self.input.read_text() + 'broken\n{"url":"file:///tmp/x"}\n')
        code, _, err = self.cli(self.args)
        self.assertEqual(code, 2)
        self.assertIn("line 2", err)
        self.assertFalse(self.frontier.exists())
        self.assertEqual(json.loads(self.report.read_text())["counts"]["unsupported"], 1)
        self.assertEqual(self.cli(self.args + ["--on-invalid", "skip"])[0], 0)
        self.assertEqual(len(self.frontier.read_text().splitlines()), 1)

    def test_rejected_policy_keeps_existing_frontier_and_reports_witnesses(self):
        self.input.write_text('{"url":"https://example.test/#a"}\n{"url":"https://example.test/#b"}\n')
        self.frontier.write_text("existing frontier\n")
        policy = self.root / "policy.json"
        policy.write_text('{"fragment":"drop"}')
        code, _, err = self.cli(self.args + ["--policy", str(policy)])
        self.assertEqual(code, 2)
        self.assertIn("allow_ambiguous_merges", err)
        self.assertEqual(self.frontier.read_text(), "existing frontier\n")
        report = json.loads(self.report.read_text())
        self.assertFalse(report["frontier_emitted"])
        self.assertEqual(len(report["collision_groups"][0]["members"]), 2)

    def test_unknown_policy_and_non_utf8_fail_clearly(self):
        policy = self.root / "policy.json"
        policy.write_text('{"sort_all":true}')
        code, _, err = self.cli(self.args + ["--policy", str(policy)])
        self.assertEqual(code, 2)
        self.assertIn("unknown policy", err)
        self.input.write_bytes(b"\xff")
        code, _, err = self.cli(self.args)
        self.assertEqual(code, 2)
        self.assertIn("decode", err)

    def test_protect_inputs_outputs_hardlinks_and_symlinks(self):
        for target in (self.input, self.report):
            code, _, err = self.cli([str(self.input), "--frontier", str(target), "--report", str(self.report)])
            self.assertEqual(code, 2)
            self.assertIn("distinct", err)
        self.frontier.hardlink_to(self.input)
        self.assertEqual(self.cli(self.args)[0], 2)
        self.frontier.unlink()
        self.frontier.symlink_to(self.input)
        self.assertEqual(self.cli(self.args)[0], 2)
        self.assertIn("https://example.test", self.input.read_text())

    def test_write_failure_cleans_temporary_file_and_keeps_original(self):
        self.frontier.write_text("old")
        with patch("qawrench.frontier.cli.os.replace", side_effect=OSError("simulated disk failure")):
            code, _, err = self.cli(self.args)
        self.assertEqual(code, 2)
        self.assertIn("simulated disk failure", err)
        self.assertEqual(self.frontier.read_text(), "old")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["frontier.jsonl", "input.jsonl"])

    def test_missing_output_directory_and_input_fail_clearly(self):
        code, _, err = self.cli([str(self.input), "--frontier", str(self.root / "missing/f"), "--report", str(self.report)])
        self.assertEqual(code, 2)
        self.assertIn("directory", err)
        self.input.unlink()
        self.assertEqual(self.cli(self.args)[0], 2)

    def test_crawlee_cli_e2e_and_jsonl_frontier_round_trip(self):
        args = [str(ROOT / "examples/frontier/crawlee-sources.json"), "--format", "crawlee",
                "--frontier", str(self.frontier), "--report", str(self.report)]
        self.assertEqual(self.cli(args)[0], 0)
        records, issues, total = load_requests(self.frontier)
        self.assertEqual((len(records), issues, total), (3, [], 3))


class ExperimentTests(unittest.TestCase):
    def test_seed_reproducibility_and_different_shuffle(self):
        self.assertEqual(seeded_records(39, 2), seeded_records(39, 2))
        self.assertNotEqual(seeded_records(39, 2), seeded_records(40, 2))

    def test_measured_comparison_detects_known_false_merges(self):
        result = run(seed=39, groups=3, repeats=2)
        self.assertTrue(result["fixture_e2e"]["passed"])
        self.assertEqual(result["results"]["tracking_only"]["false_merge_groups"], 0)
        self.assertEqual(result["results"]["conservative"]["false_merge_groups"], 0)
        self.assertEqual(result["results"]["proposed"]["false_merge_groups"], 3)
        self.assertEqual(result["results"]["proposed_with_w3lib_sort"]["false_merge_groups"], 9)
        self.assertGreater(result["results"]["proposed"]["median_milliseconds"], 0)


if __name__ == "__main__":
    unittest.main()
