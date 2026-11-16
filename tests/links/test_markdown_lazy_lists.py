"""Lazy list contracts: independent source regions and conservative boundaries."""

import json
from pathlib import Path
import tempfile
import unittest

from qawrench.links import convert, load_json


FIXTURE = Path(__file__).parent / 'fixtures/markdown-lazy-lists'
URL = 'https://x.invalid/'


class MarkdownLazyListTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report = load_json(FIXTURE / 'lychee.json')
        for source in self.report['error_map']:
            (self.root / source).write_bytes((FIXTURE / source).read_bytes())

    def run_report(self, **options):
        return convert(self.report, root=self.root, markdown=True, **options)['runs'][0]

    def regions(self, run, source='basic.md'):
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

    def test_lazy_list_regression(self):
        self.assertEqual(self.regions(self.run_report()),
                         load_json(FIXTURE / 'expected.json')['basic.md'])

    def test_independent_nested_unicode_repeated_and_reference_regions(self):
        expected = load_json(FIXTURE / 'expected.json')
        run = self.run_report()
        self.assertEqual({s: self.regions(run, s) for s in expected}, expected)
        summary = run['properties']['summary']
        self.assertEqual((summary['mappedResults'], summary['unmappedFindings'], summary['resultCount']),
                         (17, 2, 19))
        for result in run['results']:
            if not result['properties']['mapped']:
                self.assertNotIn('region', result['locations'][0]['physicalLocation'])
        self.assertEqual(len({json.dumps(r['partialFingerprints']) for r in run['results']}), 19)

    def test_crlf_default_and_matching_baseline(self):
        before = self.run_report()
        for source in self.report['error_map']:
            path = self.root / source
            path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n'))
        self.assertEqual(before, self.run_report())
        baseline = convert(self.report, root=self.root, markdown=True)
        self.assertEqual(self.run_report(baseline=baseline)['properties']['summary']['baselineComparison'],
                         {'new': 0, 'unchanged': 19, 'updated': 0, 'moved': 0, 'absent': 0})
        default = convert(self.report, root=self.root)['runs'][0]
        self.assertEqual(default['properties']['summary']['mappedResults'], 0)
        self.assertTrue(all('region' not in r['locations'][0]['physicalLocation']
                            for r in default['results']))

    def test_supported_marker_widths_and_partial_indentation(self):
        for marker in ['-', '+', '*', '1.', '2)', '999999999.']:
            with self.subTest(marker=marker):
                self.only_source(f'{marker} text\n [yes]({URL})')
                self.assertEqual(self.regions(self.run_report()), [
                    {'startLine': 2, 'startColumn': 8, 'endLine': 2, 'endColumn': 26}])
        self.only_source(f'1. - text\n   partial\n[yes]({URL})')
        self.assertEqual(self.regions(self.run_report()), [
            {'startLine': 3, 'startColumn': 7, 'endLine': 3, 'endColumn': 25}])

    def test_blank_ends_lazy_paragraph_and_allows_top_level_definition(self):
        self.only_source(f'- text\n\n[ref]: {URL}\n\n[ref]')
        self.assertEqual(self.regions(self.run_report()), [
            {'startLine': 5, 'startColumn': 1, 'endLine': 5, 'endColumn': 6}])
        # Four absolute spaces can still be prose in a two-space list item.
        self.only_source(f'- text\n\n    [yes]({URL})')
        self.assertEqual(self.regions(self.run_report()), [
            {'startLine': 3, 'startColumn': 11, 'endLine': 3, 'endColumn': 29}])

    def test_only_active_paragraphs_continue(self):
        cases = [
            f'- # heading\n[lazy]({URL})', f'-\n[lazy]({URL})',
            f'- text\n\n      [code]({URL})', f'- text\n  \n      [code]({URL})',
            f'- heading\n  ===\n[lazy]({URL})',
            f'- heading\n  ---\n[lazy]({URL})',
            f'- [definition]: {URL}\n[lazy]({URL})',
            f'-     code\n[lazy]({URL})', f'-\ttext\n[lazy]({URL})',
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assert_file_only(text)

    def test_block_interruptions_end_lazy_eligibility(self):
        for interrupt in ['# heading', '---', '===', '- # heading', '1. # heading',
                          '2. # heading', '```md', '~~~', '      code', '\tcode',
                          ' \tcode', '   \tcode', '<div>', '<script>', '<!--',
                          '<?xml', '<![CDATA[', '<!DOCTYPE html>']:
            with self.subTest(interrupt=interrupt):
                self.assert_file_only(f'- text\n{interrupt}\n[lazy]({URL})')

    def test_exclusions_keep_lazy_lines_file_only(self):
        cases = [
            f'- ```md\n[code]({URL})\n```', f'-     code\n[code]({URL})',
            f'- <div>\n[html]({URL})\n</div>',
            f'- <script>\n[html]({URL})\n</script>',
            f'- <!--\n[comment]({URL})\n-->',
            f'- text\n<!-- [comment]({URL}) -->',
            f'- ![image]({URL})\n![image]({URL})',
            f'- `open\n[code]({URL})\nclose`',
            f'- text\n![image][ref]\n\n[ref]: {URL}',
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assert_file_only(text)

    def test_lazy_prose_cannot_invent_or_override_definitions(self):
        for prefix, lazy in [('- text', ''), ('- - text', '  '),
                             ('> - text', '> '), ('- > text', ''),
                             ('- text\n    code', '')]:
            with self.subTest(prefix=prefix, lazy=lazy):
                self.assert_file_only(f'{prefix}\n{lazy}[ref]: {URL}\n\n[ref]')
                self.assert_file_only(f'{prefix}\n{lazy}[ref]: {URL}\n\n'
                                      '[ref]: https://other.invalid/\n\n[ref]')
        self.assert_file_only(f'- text\n[ref]:\n{URL}\n\n[ref]')

    def test_blank_and_explicit_markers_recover_and_reset_code_spans(self):
        for text, line, start in [
            (f'- text\n<div>\n[hidden]({URL})\n\n[yes]({URL})', 5, 7),
            (f'- text\n<div>\n[hidden]({URL})\n- [yes]({URL})', 4, 9),
            (f'- `open\n- [yes]({URL})\n- `', 2, 9),
            (f'- `open\n\n[yes]({URL})\n`', 3, 7),
        ]:
            with self.subTest(text=text):
                self.only_source(text)
                self.assertEqual(self.regions(self.run_report()), [
                    {'startLine': line, 'startColumn': start,
                     'endLine': line, 'endColumn': start + 18}])


if __name__ == '__main__':
    unittest.main()
