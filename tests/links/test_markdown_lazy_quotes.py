"""Lazy quote contracts: hand-authored regions, exclusions, no invented definitions."""

import json
from pathlib import Path
import tempfile
import unittest

from qawrench.links import convert, load_json


FIXTURE = Path(__file__).parent / 'fixtures/markdown-lazy-quotes'
URL = 'https://x.invalid/'


class MarkdownLazyQuoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report = load_json(FIXTURE / 'lychee.json')
        for source in self.report['error_map']:
            (self.root / source).write_bytes((FIXTURE / source).read_bytes())

    def run_report(self, **options):
        return convert(self.report, root=self.root, markdown=True, **options)['runs'][0]

    def regions(self, run, source):
        return [r['locations'][0]['physicalLocation']['region'] for r in run['results']
                if r['properties']['source'] == source and r['properties']['mapped']]

    def only_source(self, text):
        self.report.update(total=1, errors=1)
        self.report['error_map'] = {'basic.md': self.report['error_map']['basic.md'][:1]}
        (self.root / 'basic.md').write_text(text, encoding='utf-8')

    def assert_file_only(self, text):
        self.only_source(text)
        run = self.run_report()
        self.assertEqual(run['properties']['summary']['mappedResults'], 0)
        self.assertEqual(len(run['results']), 1)
        self.assertNotIn('region', run['results'][0]['locations'][0]['physicalLocation'])

    def test_requested_lazy_quote_regression(self):
        expected = load_json(FIXTURE / 'expected.json')['basic.md']
        self.assertEqual(self.regions(self.run_report(), 'basic.md'), expected)

    def test_independent_nested_unicode_repeated_and_reference_regions(self):
        expected = load_json(FIXTURE / 'expected.json')
        run = self.run_report()
        self.assertEqual({source: self.regions(run, source) for source in expected}, expected)
        summary = run['properties']['summary']
        self.assertEqual((summary['mappedResults'], summary['unmappedFindings'], summary['resultCount']),
                         (15, 2, 17))
        for result in run['results']:
            if not result['properties']['mapped']:
                self.assertNotIn('region', result['locations'][0]['physicalLocation'])
        self.assertEqual(len({json.dumps(r['partialFingerprints']) for r in run['results']}), 17)

    def test_crlf_default_and_matching_baseline(self):
        before = self.run_report()
        for source in self.report['error_map']:
            path = self.root / source
            path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n'))
        self.assertEqual(before, self.run_report())
        baseline = convert(self.report, root=self.root, markdown=True)
        compared = self.run_report(baseline=baseline)
        self.assertEqual(compared['properties']['summary']['baselineComparison'],
                         {'new': 0, 'unchanged': 17, 'updated': 0, 'moved': 0, 'absent': 0})
        default = convert(self.report, root=self.root)['runs'][0]
        self.assertEqual(default['properties']['summary']['mappedResults'], 0)
        self.assertTrue(all('region' not in r['locations'][0]['physicalLocation']
                            for r in default['results']))

    def test_only_active_quote_paragraphs_can_continue(self):
        cases = [
            f'> # heading\n[lazy]({URL})', f'>\n[lazy]({URL})',
            f'> text\n\n    [code]({URL})', f'> text\n>\n    [code]({URL})',
            f'> [definition]: {URL}\n[lazy]({URL})',
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assert_file_only(text)

    def test_setext_endings_and_mixed_tab_indentation_are_not_lazy_prose(self):
        cases = [
            f'> heading\n> ===\n[lazy]({URL})',
            f'> heading\n> ---\n[lazy]({URL})',
            f'> text\n \t[code]({URL})',
            f'> text\n   \t[code]({URL})',
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assert_file_only(text)

    def test_interrupting_or_unsupported_lines_end_lazy_eligibility(self):
        interrupts = ['# heading', '---', '===', '- # heading', '1. # heading', '2. item',
                      '```md', '~~~', '    code', '\tcode', '<div>', '<script>',
                      '<!--', '<?xml', '<![CDATA[', '<!DOCTYPE html>']
        for interrupt in interrupts:
            with self.subTest(interrupt=interrupt):
                self.assert_file_only(f'> text\n{interrupt}\n[lazy]({URL})')

    def test_exclusions_and_nonparagraphs_cannot_enable_lazy_prose(self):
        cases = [
            f'> ```md\n[code]({URL})\n```', f'>     code\n[code]({URL})',
            f'> <div>\n[html]({URL})\n</div>',
            f'> <script>\n[html]({URL})\n</script>',
            f'> <!--\n[comment]({URL})\n-->',
            f'> text\n<!-- [comment]({URL}) -->',
            f'> ![image]({URL})\n![image]({URL})',
            f'> `open\n[code]({URL})\nclose`',
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assert_file_only(text)

    def test_lazy_prose_never_promotes_reference_definitions(self):
        cases = [
            f'> text\n[ref]: {URL}\n\n[ref]',
            f'> text\n[ref]: {URL}\n> [ref]\n\n[ref]',
            f'> > text\n> [ref]: {URL}\n\n[ref]',
            f'- > text\n  [ref]: {URL}\n\n[ref]',
            f'> # heading\n[ref]: {URL}\n\n[ref]',
            f'> text\n---\n[ref]: {URL}\n\n[ref]',
            f'> text\n[ref]:\n{URL}\n\n[ref]',
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assert_file_only(text)

    def test_lazy_definition_spelling_does_not_override_first_real_definition(self):
        self.only_source(f'> text\n[ref]: {URL}\n\n[ref]: https://other.invalid/\n\n[ref]')
        self.assertEqual(self.run_report()['properties']['summary']['mappedResults'], 0)

    def test_blank_and_explicit_container_recover_after_rejected_lazy_line(self):
        for recovery in ['\n', '> ']:
            with self.subTest(recovery=recovery):
                self.only_source(f'> text\n<div>\n[hidden]({URL})\n{recovery}[yes]({URL})')
                self.assertEqual(self.regions(self.run_report(), 'basic.md'), [
                    {'startLine': 5 if recovery == '\n' else 4,
                     'startColumn': 7 if recovery == '\n' else 9,
                     'endLine': 5 if recovery == '\n' else 4,
                     'endColumn': 25 if recovery == '\n' else 27}])


if __name__ == '__main__':
    unittest.main()
