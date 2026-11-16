"""Independent synthetic Markdown coordinates; expected spans are not parser-derived."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from qawrench.links import InputError, convert, load_json

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/links/fixtures/markdown"
SOURCE = "docs/café #100%.md"


def report_for(urls, source=SOURCE):
    report = load_json(ROOT / "examples/links/lychee.json")
    report.update(total=len(urls), successful=0, errors=len(urls), timeouts=0)
    report["error_map"] = {source: [
        {"url": url, "status": {"text": "Network error", "code": 404}} for url in urls]}
    return report


class MarkdownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "docs").mkdir()
        self.path = self.root / SOURCE
        self.path.write_bytes((FIXTURES / "links.md").read_bytes())
        self.expected = load_json(FIXTURES / "expected.json")
        self.report = report_for([*self.expected, "https://unmatched.invalid/"])

    def run_report(self):
        return convert(self.report, root=self.root, markdown=True)["runs"][0]

    def test_exact_inline_reference_and_repeated_regions(self):
        run = self.run_report()
        actual = {url: [] for url in self.expected}
        for result in run["results"]:
            props = result["properties"]
            location = result["locations"][0]["physicalLocation"]
            self.assertEqual(props["source"], SOURCE)
            self.assertEqual(location["artifactLocation"]["uri"], "docs/caf%C3%A9%20%23100%25.md")
            url = props["originalEntry"]["url"]
            if url in actual:
                actual[url].append(location["region"])
            else:
                self.assertNotIn("region", location)
        self.assertEqual(actual, self.expected)
        self.assertEqual(run["properties"]["summary"]["mappedResults"], 7)
        self.assertEqual(run["properties"]["summary"]["unmappedFindings"], 1)
        self.assertEqual(len({json.dumps(r["partialFingerprints"]) for r in run["results"]}), 8)
        self.assertEqual(self.report, report_for([*self.expected, "https://unmatched.invalid/"]))

    def test_crlf_columns_and_order_are_stable(self):
        original = self.run_report()
        self.path.write_bytes(self.path.read_bytes().replace(b"\n", b"\r\n"))
        self.report["error_map"][SOURCE].reverse()
        self.assertEqual(original, self.run_report())

    def test_mode_is_explicit_and_excludes_manifest(self):
        default = convert(self.report, root=self.root)["runs"][0]
        self.assertEqual(default["properties"]["summary"]["mappedResults"], 0)
        with self.assertRaisesRegex(InputError, "manifest.*markdown|markdown.*manifest"):
            convert(self.report, root=self.root, markdown=True, manifest=[])

    def test_reference_first_definition_and_exact_url_identity(self):
        self.path.write_text(
            '[ref][ MiXeD   label ]\n\n'
            '[mixed label]: https://EXAMPLE.invalid/%28x%29\n'
            '[MIXED LABEL]: https://example.invalid/(x)\n', encoding="utf-8")
        self.report = report_for(["https://EXAMPLE.invalid/%28x%29", "https://example.invalid/(x)"])
        run = self.run_report()
        mapped = [r for r in run["results"] if r["properties"]["mapped"]]
        self.assertEqual(len(mapped), 1)
        self.assertEqual(mapped[0]["properties"]["originalEntry"]["url"], "https://EXAMPLE.invalid/%28x%29")
        self.assertEqual(mapped[0]["locations"][0]["physicalLocation"]["region"],
                         {"startLine": 1, "startColumn": 1, "endLine": 1, "endColumn": 23})

    def test_no_cross_source_mapping_or_source_rewriting(self):
        other = "docs/alias.md"
        (self.root / other).symlink_to(self.path.name)
        self.report["error_map"][other] = deepcopy(self.report["error_map"][SOURCE])
        self.report["total"] *= 2
        self.report["errors"] *= 2
        run = self.run_report()
        for source in (SOURCE, other):
            results = [r for r in run["results"] if r["properties"]["source"] == source]
            self.assertEqual(len(results), 8)
        self.assertEqual(len({json.dumps(r["partialFingerprints"]) for r in run["results"]}), 16)

    def test_non_links_unsupported_blocks_and_exact_boundaries_stay_unmapped(self):
        url = "https://example.invalid/a(b)"
        self.report = report_for([url])
        cases = [
            f"`[code]({url})`", f"``[code]({url})``",
            f"`code\n[code]({url})\n`", f"~~~md\n[code]({url})\n~~~",
            f"```md\n[unclosed fence]({url})", f"    [indent]({url})",
            f"<div>\n[html]({url})\n</div>",
            f"<ScRiPt>\n\n[script]({url})\n</ScRiPt>",
            f"<?xml\n\n[processing instruction]({url})\n?>",
            f"<![CDATA[\n\n[cdata]({url})\n]]>",
            f"before <!--\n[comment]({url})\n--> after",
            f'html <i title=\"> [attribute]({url})\">',
            f"\\[escaped]({url})", f"![image]({url})", f"{url}",
            f"[near]({url}/suffix)", f"[bad]({url} extra)",
            f"[undefined][nope]\n\n[unused]: {url}",
            f"[outer [inner]][ref]\n\n[ref]: {url}",
            f"[ref][ ]\n\n[ref]: {url}",
            f"[ref][{'x' * 1000}]\n\n[ref]: {url}",
            f"[ref]\n[ref]: {url}",  # A definition cannot interrupt prose.
            f"[ref]\n\n[ref]:\n  {url}",  # Multiline definitions unsupported.
        ]
        for text in cases:
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                run = self.run_report()
                self.assertEqual(run["properties"]["summary"]["mappedResults"], 0)
                self.assertEqual(len(run["results"]), 1)

    def test_escaped_punctuation_angles_titles_and_reference_labels(self):
        url = "https://example.invalid/a(b)"
        self.report = report_for([url])
        cases = [
            (f'[x]({url} "title")', 5, 33),
            (f"[x](<{url}> 'title')", 6, 34),
            (f"[x]({url} (title))", 5, 33),
            ("[x](https://example.invalid/a\\(b\\))", 5, 35),
            (f"[Ref][]\n\n[ref]: {url}", 1, 8),
            (f"[ref]\n\n[REF]: {url}", 1, 6),
            (f"[x][Straße]\n\n[STRASSE]: {url}", 1, 12),
        ]
        for text, start, end in cases:
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                result = self.run_report()["results"][0]
                self.assertEqual(result["locations"][0]["physicalLocation"]["region"],
                                 {"startLine": 1, "startColumn": start, "endLine": 1, "endColumn": end})

    def test_cli_markdown_and_conflicting_modes(self):
        report_path = self.root / "lychee.json"
        report_path.write_text(json.dumps(self.report), encoding="utf-8")
        output = self.root / "out.sarif"
        base = [sys.executable, "-m", "qawrench.links", str(report_path),
                "--root", str(self.root), "--markdown", "--out", str(output)]
        result = subprocess.run(base, cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stderr)["mappedResults"], 7)
        self.assertEqual(load_json(output)["runs"][0], self.run_report())
        before = output.read_bytes()
        result = subprocess.run([*base, "--manifest", str(ROOT / "examples/links/occurrences.json")],
                                cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(output.read_bytes(), before)
        output.unlink()
        result = subprocess.run([*base, "--dry-run"], cwd=ROOT,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(json.loads(result.stderr)["mappedResults"], 7)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
