"""Independent source regions and precedence contracts for inline link tails."""

import json
from pathlib import Path
import tempfile
import unittest

from qawrench.links import convert, load_json


FIXTURE = Path(__file__).parent / 'fixtures/markdown-title-boundaries'
URL = 'https://x.invalid/'


class MarkdownTitleBoundaryTests(unittest.TestCase):
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

    def test_quoted_title_regression(self):
        self.assertEqual(self.regions(self.run_report()),
                         load_json(FIXTURE / 'expected.json')['basic.md'])

    def test_independent_images_references_containers_and_unicode_regions(self):
        expected = load_json(FIXTURE / 'expected.json')
        run = self.run_report()
        self.assertEqual({s: self.regions(run, s) for s in expected}, expected)
        summary = run['properties']['summary']
        self.assertEqual((summary['mappedResults'], summary['unmappedFindings'], summary['resultCount']),
                         (11, 2, 13))
        for result in run['results']:
            if not result['properties']['mapped']:
                self.assertNotIn('region', result['locations'][0]['physicalLocation'])
        self.assertEqual(len({json.dumps(r['partialFingerprints']) for r in run['results']}), 13)

    def test_crlf_default_and_matching_baseline(self):
        before = self.run_report()
        for source in self.report['error_map']:
            path = self.root / source
            path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n'))
        self.assertEqual(before, self.run_report())
        baseline = convert(self.report, root=self.root, markdown=True)
        self.assertEqual(self.run_report(baseline=baseline)['properties']['summary']['baselineComparison'],
                         {'new': 0, 'unchanged': 13, 'updated': 0, 'moved': 0, 'absent': 0})
        default = convert(self.report, root=self.root)['runs'][0]
        self.assertEqual(default['properties']['summary']['mappedResults'], 0)
        self.assertEqual(len(default['results']), 4)

    def test_combining_marks_and_tabs_count_original_code_points(self):
        self.only_source(f'e\u0301\t[one]({URL} "a`") [two]({URL} "b`")')
        self.assertEqual(self.regions(self.run_report()), [
            {'startLine': 1, 'startColumn': 10, 'endLine': 1, 'endColumn': 28},
            {'startLine': 1, 'startColumn': 41, 'endLine': 1, 'endColumn': 59},
        ])

    def test_supported_title_forms_and_label_code_keep_later_links_visible(self):
        for title in ['"a`"', "'a`'", '(a`)', '"a\\\"`"', "'a\\'`'", '(a\\)`)', '"``a"']:
            for label in ['one', 'a`b`c', r'a\[b\]c']:
                with self.subTest(title=title, label=label):
                    self.only_source(f'[{label}](<{URL}> {title}) [two]({URL} {title})')
                    self.assertEqual(len(self.regions(self.run_report())), 2)

    def test_inline_image_tails_are_atomic_but_images_never_map(self):
        for prefix in ['', '> ', '- ', '> - ']:
            with self.subTest(prefix=prefix):
                self.only_source(f'{prefix}![image]({URL} "a`") [two]({URL} "b`")')
                self.assertEqual(len(self.regions(self.run_report())), 1)
                self.assert_file_only(f'{prefix}![image]({URL} "a`") ![two]({URL} "b`")')

    def test_bare_and_angle_destinations_are_atomic(self):
        # Backticks are literal destination characters too; no URL normalization.
        for first in [f'{URL}a`', f'<{URL}a`>']:
            with self.subTest(first=first):
                self.only_source(f'[other]({first}) [two]({URL} "b`")')
                self.assertEqual(len(self.regions(self.run_report())), 1)

    def test_prior_code_openers_in_prose_or_labels_keep_precedence(self):
        for text in [f'`[one]({URL} "a`")', f'[a`b]({URL} "a`")',
                     f'``[one]({URL} "a``")', f'[a``b]({URL} "a``")']:
            with self.subTest(text=text):
                self.assert_file_only(text)
        # Earlier prose opener closes inside a later title, then prose resumes.
        self.only_source(f'`[one]({URL} "a`") [two]({URL})')
        self.assertEqual(len(self.regions(self.run_report())), 1)
        self.only_source(f'`[one]({URL} "a`") [two]({URL} "b`")`')
        self.assertEqual(len(self.regions(self.run_report())), 1)
        for prefix, continuation in [('', ''), ('> ', '> '), ('- ', '  '),
                                     ('> - ', '>   '), ('> ', ''), ('- ', '')]:
            with self.subTest(prefix=prefix, continuation=continuation):
                self.assert_file_only(f'{prefix}`open\n{continuation}[one]({URL} "a`")')

    def test_invalid_titles_remain_prose_for_code_span_pairing(self):
        for bad in ['"a`" extra', "'a`' extra", '(a`(nested))', '"a`', "'a`", '(a`',
                    '"a`\ncontinued"']:
            with self.subTest(bad=bad):
                self.only_source(f'[bad]({URL} {bad}) [two]({URL} "b`")')
                self.assertEqual(self.regions(self.run_report()), [])
        self.assert_file_only(f'[bad]({URL} "a`\ncontinued") [two]({URL} "b`")')

    def test_block_code_html_comments_and_lazy_definitions_stay_excluded(self):
        pair = f'[one]({URL} "a`") [two]({URL} "b`")'
        for text in [f'``{pair}``', f'~~~\n{pair}\n~~~',
                     f'    {pair}', f'> ```\n> {pair}\n> ```', f'-     {pair}',
                     f'<!-- {pair} -->', f'<div>\n{pair}\n</div>',
                     f'> <script>\n> {pair}\n> </script>',
                     f'- start\n[ref]: {URL} "a`"\n\n[ref]',
                     f'> start\n[ref]: {URL} "a`"\n\n[ref]']:
            with self.subTest(text=text):
                self.assert_file_only(text)


if __name__ == '__main__':
    unittest.main()
