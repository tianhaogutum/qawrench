"""Independent heading regions and code-span scope at supported block boundaries."""

import json
from pathlib import Path
import tempfile
import unittest

from qawrench.links import convert, load_json


FIXTURE = Path(__file__).parent / 'fixtures/markdown-heading-boundaries'
URL = 'https://x.invalid/'


class MarkdownHeadingBoundaryTests(unittest.TestCase):
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

    def test_heading_boundary_regression(self):
        self.assertEqual(self.regions(self.run_report()),
                         load_json(FIXTURE / 'expected.json')['basic.md'])

    def test_independent_into_out_of_adjacent_and_nested_heading_regions(self):
        expected = load_json(FIXTURE / 'expected.json')
        run = self.run_report()
        self.assertEqual({s: self.regions(run, s) for s in expected}, expected)
        summary = run['properties']['summary']
        self.assertEqual((summary['mappedResults'], summary['unmappedFindings'], summary['resultCount']),
                         (10, 2, 12))
        for result in run['results']:
            if not result['properties']['mapped']:
                self.assertNotIn('region', result['locations'][0]['physicalLocation'])
        self.assertEqual(len({json.dumps(r['partialFingerprints']) for r in run['results']}), 12)

    def test_crlf_default_and_matching_baseline(self):
        before = self.run_report()
        for source in self.report['error_map']:
            path = self.root / source
            path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n'))
        self.assertEqual(before, self.run_report())
        baseline = convert(self.report, root=self.root, markdown=True)
        self.assertEqual(self.run_report(baseline=baseline)['properties']['summary']['baselineComparison'],
                         {'new': 0, 'unchanged': 12, 'updated': 0, 'moved': 0, 'absent': 0})
        default = convert(self.report, root=self.root)['runs'][0]
        self.assertEqual(default['properties']['summary']['mappedResults'], 0)
        self.assertEqual(len(default['results']), 4)
        self.assertTrue(all('region' not in r['locations'][0]['physicalLocation']
                            for r in default['results']))

    def test_all_atx_levels_split_code_spans_with_original_prefix_columns(self):
        for prefix, continuation in [('', ''), ('> ', '> '), ('- ', '  '),
                                     ('> - ', '>   '), ('- > ', '  > ')]:
            for level in range(1, 7):
                with self.subTest(prefix=prefix, level=level):
                    heading = '#' * level
                    for text, line, column in [
                        (f'{prefix}`open\n{continuation}{heading}\t[x]({URL})\n'
                         f'{continuation}close`', 2, len(continuation) + level + 6),
                        (f'{prefix}{heading} `open\n{continuation}[x]({URL})\n'
                         f'{continuation}close`', 2, len(continuation) + 5),
                    ]:
                        self.only_source(text)
                        self.assertEqual(self.regions(self.run_report()), [
                            {'startLine': line, 'startColumn': column,
                             'endLine': line, 'endColumn': column + 18}])
        # Empty headings also terminate an inline block.
        self.only_source(f'`open\n#\n[x]({URL})\nclose`')
        self.assertEqual(self.regions(self.run_report()), [
            {'startLine': 3, 'startColumn': 5, 'endLine': 3, 'endColumn': 23}])

    def test_valid_paragraph_code_spans_still_cross_explicit_and_lazy_lines(self):
        for prefix, continuation in [('', ''), ('> ', '> '), ('- ', '  '),
                                     ('> - ', '>   '), ('- > ', '  > '),
                                     ('> ', ''), ('- ', ''), ('> - ', '> '),
                                     ('- > ', '')]:
            with self.subTest(prefix=prefix, continuation=continuation):
                self.assert_file_only(f'{prefix}`open\n{continuation}[x]({URL})\n'
                                      f'{continuation}close`')
        # These spellings remain ordinary prose, so they cannot reset spans.
        for text in ['####### ', '#no-space ', r'\# ', 'text # ']:
            with self.subTest(text=text):
                self.assert_file_only(f'`open\n{text}[x]({URL})\nclose`')

    def test_heading_code_images_and_block_exclusions_remain_file_only(self):
        for text in [
            f'# `[x]({URL})`', f'> ## ``[x]({URL})``',
            f'- ### ![image]({URL})',
            f'> # ![image][ref]\n\n[ref]: {URL}',
            f'> ```\n> # [x]({URL})\n> ```',
            f'- ~~~\n  # [x]({URL})\n  ~~~',
            f'-     # [x]({URL})', f'    # [x]({URL})',
            f'> <!--\n> # [x]({URL})\n> -->',
            f'> <div>\n> # [x]({URL})\n> </div>',
            f'- <script>\n  # [x]({URL})\n  </script>',
        ]:
            with self.subTest(text=text):
                self.assert_file_only(text)

    def test_heading_does_not_enable_lazy_prose_or_invent_definitions(self):
        for text in [
            f'> # heading\n[x]({URL})', f'- # heading\n[x]({URL})',
            f'> - ## heading\n> [x]({URL})',
            f'# [ref]: {URL}\n[ref]',
            f'> # heading\n[ref]: {URL}\n\n[ref]',
            f'- # heading\n[ref]: {URL}\n\n[ref]',
            f'> # `[ref]: {URL}`\n> [ref]',
            f'# `[ref]: {URL}`\n[ref]',
        ]:
            with self.subTest(text=text):
                self.assert_file_only(text)


if __name__ == '__main__':
    unittest.main()
