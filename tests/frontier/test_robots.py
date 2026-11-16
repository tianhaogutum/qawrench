"""Synthetic offline robots snapshots; no HTTP server or credentials required."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qawrench.frontier import AuditError, Policy, RobotsPolicy, audit
from qawrench.frontier.cli import main
from qawrench.frontier.core import parse_request


class RobotsIntegrationTests(unittest.TestCase):
    def test_offline_audit_reports_decisions_provenance_and_missing_origins(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            input_path, manifest = root / "requests.jsonl", root / "robots.json"
            urls = ["https://example.test/private/x", "https://EXAMPLE.test:443/private/public",
                    "http://example.test/private", "https://down.test/x"]
            input_path.write_text("".join(json.dumps({"url": u}) + "\n" for u in urls))
            manifest.write_text(json.dumps({"version": 1, "origins": {
                "https://example.test": {"status": "available", "source": "synthetic:snapshot",
                    "text": "User-agent: AuditBot\nDisallow: /private\nAllow: /private/public\n"},
                "https://down.test": {"status": "unavailable", "source": "synthetic:failed-export",
                    "reason": "snapshot not captured"}}}))
            out = io.StringIO()
            # Any attempted networking is a failure, including DNS or sockets.
            with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                    patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden")), \
                    contextlib.redirect_stdout(out):
                code = main([str(input_path), "--robots", str(manifest), "--user-agent", "AuditBot", "--dry-run"])
            self.assertEqual(code, 0)
            report = json.loads(out.getvalue())
            robots = report["robots"]
            self.assertEqual(robots["user_agent"], "AuditBot")
            self.assertEqual(robots["counts"], {"allow": 1, "disallow": 1, "unknown": 2})
            origins = robots["origins"]
            self.assertEqual(len(origins), 3)
            self.assertEqual(origins[0]["source"], "synthetic:snapshot")
            self.assertEqual(origins[0]["requests"][0]["decision"], "disallow")
            self.assertEqual(origins[0]["requests"][0]["winning_rule"]["line"], 2)
            self.assertEqual(origins[0]["requests"][1]["winning_rule"]["line"], 3)
            self.assertEqual(origins[1]["status"], "unknown")
            self.assertEqual(origins[2]["status"], "unavailable")
            self.assertEqual(origins[2]["reason"], "snapshot not captured")
            self.assertEqual(report["counts"]["accepted"], 4)  # audit does not filter


class RobotsMatchingTests(unittest.TestCase):
    def evaluate(self, text, paths, agent="AuditBot"):
        records = [parse_request({"url": "https://example.test" + path}, i, f"line {i}", "jsonl")
                   for i, path in enumerate(paths, 1)]
        policy = RobotsPolicy.from_dict({"version": 1, "origins": {
            "https://example.test": {"status": "available", "source": "synthetic:test", "text": text}}})
        frontier, report = audit(records, Policy(), robots=policy, user_agent=agent)
        self.assertEqual([r["url"] for r in frontier], [r.url for r in records])
        self.assertEqual(audit(records, Policy(), robots=policy, user_agent=agent)[1], report)
        return report["robots"]["origins"][0]

    def test_exact_case_insensitive_groups_merge_and_override_wildcard(self):
        output = self.evaluate("User-agent: *\nDisallow: /\n"
            "User-agent: auditbot\nUser-agent: PeerBot\nDisallow: /one\n"
            "User-agent: AUDITBOT\nDisallow: /two\n", ["/one", "/two", "/free"])
        self.assertEqual([r["decision"] for r in output["requests"]], ["disallow", "disallow", "allow"])
        self.assertEqual([a["line"] for a in output["selected_agents"]], [3, 4, 6])

    def test_wildcard_fallback_merges_groups_and_no_substring_agent_match(self):
        output = self.evaluate("User-agent: *\nDisallow: /one\nUser-agent: Audit\nDisallow: /\n"
                               "User-agent: *\nDisallow: /two\n", ["/one", "/two", "/free"])
        self.assertEqual([r["decision"] for r in output["requests"]], ["disallow", "disallow", "allow"])
        self.assertEqual([a["value"] for a in output["selected_agents"]], ["*", "*"])

    def test_pattern_operators_participate_in_specificity(self):
        # Also exercises the precedence examples from Google's public REP docs.
        for text, path, expected in [
                ("Allow: /page\nDisallow: /*.htm", "/page.htm", "disallow"),
                ("Allow: /page\nDisallow: /*.ph", "/page.php", "allow"),
                ("Allow: /root\nDisallow: /root$", "/root", "disallow")]:
            with self.subTest(text=text):
                result = self.evaluate("User-agent: *\n" + text, [path])["requests"][0]
                self.assertEqual(result["decision"], expected)

    def test_bom_and_cr_line_endings_preserve_first_group_and_line_numbers(self):
        output = self.evaluate("\ufeffUser-agent: AuditBot\rDisallow: /blocked\r", ["/blocked"])
        self.assertEqual(output["requests"][0]["decision"], "disallow")
        self.assertEqual(output["requests"][0]["winning_rule"]["line"], 2)

    def test_no_selected_group_and_empty_group_allow(self):
        for text in ("", "User-agent: OtherBot\nDisallow: /\n", "User-agent: AuditBot\nDisallow:\nAllow:\n"):
            with self.subTest(text=text):
                result = self.evaluate(text, ["/anything"])["requests"][0]
                self.assertEqual(result["decision"], "allow")
                self.assertIsNone(result["winning_rule"])

    def test_longest_rule_and_allow_tie_ignore_order(self):
        text = "User-agent: *\nDisallow: /\nAllow: /private/public\nDisallow: /private\nDisallow: /private/public\n"
        results = self.evaluate(text, ["/private/x", "/private/public", "/PRIVATE/public"])["requests"]
        self.assertEqual([r["decision"] for r in results], ["disallow", "allow", "disallow"])
        self.assertEqual(results[1]["winning_rule"]["line"], 3)
        self.assertEqual(len(results[1]["matched_rules"]), 4)

    def test_wildcards_end_anchor_query_and_fragment(self):
        text = "User-agent: *\nDisallow: /*.pdf$\nDisallow: /search?secret=\nDisallow: /a*b*c$\n"
        paths = ["/a.pdf", "/a.pdf?x=1", "/a.pdf#fragment", "/search?secret=1#x", "/search?ok=1",
                 "/ab-middle-c", "/ab-middle-c-more", "/abc", "/a/x/b/y/c"]
        results = self.evaluate(text, paths)["requests"]
        self.assertEqual([r["decision"] for r in results],
                         ["disallow", "allow", "disallow", "disallow", "allow", "disallow", "allow", "disallow", "disallow"])
        self.assertEqual(results[3]["target"], "/search?secret=1")

    def test_rfc9309_percent_octets_and_literal_operators(self):
        text = ("User-agent: *\nDisallow: /café\nDisallow: /foo/%62ar\nDisallow: /a%2Fb\n"
                "Disallow: /literal/%2A\nDisallow: /dollar/%24$\nDisallow: /hash/%23\n")
        paths = ["/caf%C3%A9", "/café", "/foo/bar", "/foo/%62ar", "/a%2fb", "/a/b",
                 "/literal/*", "/literal/x", "/dollar/$", "/dollar/$x", "/hash/%23"]
        results = self.evaluate(text, paths)["requests"]
        self.assertEqual([r["decision"] for r in results],
                         ["disallow", "disallow", "disallow", "disallow", "disallow", "allow",
                          "disallow", "allow", "disallow", "allow", "disallow"])

    def test_parseable_rules_survive_invalid_records_with_line_diagnostics(self):
        text = ("Disallow: /orphan\r\nUser-agent: *\r\nSitemap: https://example.test/map.xml\r\n"
                "Disallow: /valid # comment\r\nDisallow: no-slash\r\nDisallow: /bad%xx\r\n"
                "future: extension\r\nAllow: /valid/public\r\n")
        output = self.evaluate(text, ["/valid", "/valid/public", "/orphan"])
        self.assertEqual([r["decision"] for r in output["requests"]], ["disallow", "allow", "allow"])
        self.assertEqual([d["line"] for d in output["diagnostics"]], [1, 3, 5, 6, 7])
        self.assertEqual(output["requests"][1]["winning_rule"]["line"], 8)

    def test_robots_itself_implicitly_allowed_and_empty_url_path_is_root(self):
        output = self.evaluate("User-agent: *\nDisallow: /\n", ["", "/robots.txt", "/robots.txt?x=1", "/%72obots.txt", "/Robots.txt"])
        self.assertEqual([r["decision"] for r in output["requests"]], ["disallow", "allow", "allow", "allow", "disallow"])

    def test_original_request_is_audited_even_when_identity_policy_drops_query(self):
        records = [parse_request({"url": "https://example.test/?secret=yes"}, 1, "line 1", "jsonl")]
        robots = RobotsPolicy.from_dict({"version": 1, "origins": {"https://example.test": {
            "status": "available", "text": "User-agent: *\nDisallow: /?secret=", "source": "synthetic:test"}}})
        _, report = audit(records, Policy(remove_query_keys=("secret",)), robots=robots, user_agent="AuditBot")
        self.assertEqual(report["robots"]["counts"]["disallow"], 1)
        self.assertEqual(report["changed_key_witnesses"][0]["proposed_url"], "https://example.test/")


class RobotsValidationTests(unittest.TestCase):
    def test_origin_scope_canonicalization_and_duplicate_detection(self):
        from qawrench.frontier.robots import origin_for
        for given, expected in [("HTTPS://Bücher.test:00443/a", "https://xn--bcher-kva.test"),
                                ("http://EXAMPLE.test:80/a", "http://example.test"),
                                ("https://example.test:80/a", "https://example.test:80"),
                                ("http://[2001:0db8::1]:0080/a", "http://[2001:db8::1]")]:
            with self.subTest(given=given):
                self.assertEqual(origin_for(given), expected)
        entry = {"status": "available", "source": "test", "text": ""}
        with self.assertRaisesRegex(AuditError, "duplicate normalized"):
            RobotsPolicy.from_dict({"version": 1, "origins": {"https://EXAMPLE.test:443": entry,
                                                               "https://example.test/": entry}})

    def test_manifest_rejects_unsupported_schema_and_bad_unicode(self):
        bad = [None, [], {}, {"version": True, "origins": {}}, {"version": 2, "origins": {}},
               {"version": 1, "origins": []}, {"version": 1, "origins": {}, "extra": 1}]
        entries = [None, {"status": "future"}, {"status": "available", "source": "x", "text": None},
                   {"status": "available", "source": "", "text": ""},
                   {"status": "available", "source": "x", "text": "\ud800"},
                   {"status": "available", "source": "x", "text": "", "url": "https://example.test/robots.txt"},
                   {"status": "unavailable", "source": "x", "reason": ""},
                   {"status": "unavailable", "source": "x", "reason": "gone", "text": ""}]
        bad.extend({"version": 1, "origins": {"https://example.test": e}} for e in entries)
        for origin in ("https://example.test/path", "https://example.test?", "https://example.test/#",
                       "file:///robots.txt", "https://user@example.test"):
            bad.append({"version": 1, "origins": {origin: {"status": "available", "source": "x", "text": ""}}})
        for value in bad:
            with self.subTest(value=value), self.assertRaises(AuditError):
                RobotsPolicy.from_dict(value)

    def test_library_requires_manifest_and_valid_explicit_product_token(self):
        robots = RobotsPolicy.from_dict({"version": 1, "origins": {}})
        for agent in ("*", "", "AuditBot/1.0", "Audit Bot", "Bot1", "ÄBot"):
            with self.subTest(agent=agent), self.assertRaises(AuditError):
                audit([], Policy(), robots=robots, user_agent=agent)
        for kwargs in ({"robots": robots}, {"user_agent": "AuditBot"}):
            with self.assertRaisesRegex(AuditError, "together"):
                audit([], Policy(), **kwargs)

    def test_cli_failure_is_no_write_and_manifest_is_protected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            input_path, manifest = root / "input.jsonl", root / "robots.json"
            frontier, report = root / "frontier", root / "report"
            input_path.write_text('{"url":"https://example.test/"}\n')
            manifest.write_text('{"version":1,"origins":{}}')
            frontier.write_text("old frontier")
            report.write_text("old report")
            args = [str(input_path), "--frontier", str(frontier), "--report", str(report)]
            for extra in (["--robots", str(manifest)], ["--user-agent", "AuditBot"],
                          ["--robots", str(manifest), "--user-agent", "*"],
                          ["--robots", str(root / "missing"), "--user-agent", "AuditBot"]):
                with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(args + extra), 2)
                self.assertEqual(frontier.read_text(), "old frontier")
                self.assertEqual(report.read_text(), "old report")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main([str(input_path), "--frontier", str(manifest), "--report", str(report),
                                       "--robots", str(manifest), "--user-agent", "AuditBot"]), 2)
            self.assertEqual(manifest.read_text(), '{"version":1,"origins":{}}')
            manifest.write_text('{"version":1,"origins":{},"origins":{}}')
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(args + ["--robots", str(manifest), "--user-agent", "AuditBot"]), 2)
            self.assertEqual(frontier.read_text(), "old frontier")
            self.assertEqual(report.read_text(), "old report")


if __name__ == "__main__":
    unittest.main()
