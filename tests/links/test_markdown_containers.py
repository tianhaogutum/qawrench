"""Container contract: independently specified original-source regions."""

import json
from pathlib import Path
import tempfile
import unittest

from qawrench.links import convert, load_json


FIXTURE = Path(__file__).parent / 'fixtures/markdown-containers'
URL = 'https://x.invalid/'


class MarkdownContainerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report = load_json(FIXTURE / 'lychee.json')
        for source in self.report['error_map']:
            (self.root / source).write_bytes((FIXTURE / source).read_bytes())

    def run_report(self, **options):
        return convert(self.report, root=self.root, markdown=True, **options)['runs'][0]

    def only_source(self, text):
        self.report.update(total=1, errors=1)
        self.report['error_map'] = {'nested.md': self.report['error_map']['nested.md'][:1]}
        (self.root / 'nested.md').write_text(text, encoding='utf-8')

    def test_nested_inline_and_independent_repeated_reference_regions(self):
        run = self.run_report()
        expected = load_json(FIXTURE / 'expected.json')
        actual = {source: [] for source in expected}
        for result in run['results']:
            physical = result['locations'][0]['physicalLocation']
            self.assertEqual(physical['artifactLocation']['uri'], result['properties']['source'])
            if result['properties']['originalEntry']['url'] == URL:
                self.assertTrue(result['properties']['mapped'])
                actual[result['properties']['source']].append(physical['region'])
            else:
                self.assertNotIn('region', physical)
        self.assertEqual(actual, expected)
        summary = run['properties']['summary']
        self.assertEqual((summary['mappedResults'], summary['unmappedFindings'], summary['resultCount']),
                         (15, 2, 17))
        self.assertEqual(len({json.dumps(r['partialFingerprints']) for r in run['results']}), 17)
        default = convert(self.report, root=self.root)['runs'][0]
        self.assertEqual(default['properties']['summary']['mappedResults'], 0)

    def test_crlf_and_matching_baseline_keep_original_regions(self):
        before = self.run_report()
        for source in self.report['error_map']:
            path = self.root / source
            path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n'))
        self.assertEqual(before, self.run_report())
        baseline = convert(self.report, root=self.root, markdown=True)
        compared = self.run_report(baseline=baseline)
        self.assertEqual(compared['properties']['summary']['baselineComparison'],
                         {'new': 0, 'unchanged': 17, 'updated': 0, 'moved': 0, 'absent': 0})

    def test_unsupported_content_stays_file_only_and_definitions_do_not_leak(self):
        cases = [
            f'> ```md\n> [code]({URL})\n> ```',
            f'> ```md\n> - [code]({URL})\n> ```',
            f'- ~~~\n  [code]({URL})\n  ~~~',
            f'- ~~~\n  > [code]({URL})\n  ~~~',
            f'> -     [indented]({URL})', f'- item\n      [indented]({URL})',
            f'> `[code]({URL})`', f'- `code\n  [code]({URL})\n  `',
            f'> ![image]({URL})', f'> <!--\n> [comment]({URL})\n> -->',
            f'- <div>\n  [html]({URL})\n  </div>',
            f'> <script>\n>\n> [script]({URL})\n> </script>',
            f'> <script>\n> - [script]({URL})\n> </script>',
            f'-\t[tab]({URL})', f'>\t[tab]({URL})',
            f'- text\n\t[tab]({URL})',
            f'> [ref]\n> [ref]: {URL}',
            f'- [ref]\n  [ref]: {URL}',
            f'text\n2. [ref]: {URL}\n\n[ref]',
            f'- text\n  2. [ref]: {URL}\n\n[ref]',
            f'- text\n2. [ref]: {URL}\n\n[ref]',
            f'> ```\n> [ref]: {URL}\n> ```\n\n[ref]',
            f'-     [ref]: {URL}\n\n[ref]',
            f'> <!--\n> [ref]: {URL}\n> -->\n\n[ref]',
            f'> [near]({URL}suffix)', f'> [bad]({URL} extra)',
            f'> [ref]\n>\n> [ref]:\n>   {URL}',
        ]
        for text in cases:
            with self.subTest(text=text):
                self.only_source(text)
                run = self.run_report()
                self.assertEqual(run['properties']['summary']['mappedResults'], 0)
                self.assertEqual(len(run['results']), 1)
                self.assertNotIn('region', run['results'][0]['locations'][0]['physicalLocation'])

    def test_thematic_break_is_not_a_reference_use(self):
        # A break must not open list items that strip code indentation below it.
        self.only_source(f'- - -\n      [ref]: {URL}\n\n[ref]')
        self.assertEqual(self.run_report()['properties']['summary']['mappedResults'], 0)

    def test_sibling_container_boundaries_reset_exclusions_and_code_spans(self):
        cases = [
            f'> ```\n\n[yes]({URL})',
            f'- ```\n- [yes]({URL})',
            f'- <div>\n- [yes]({URL})',
            f'> `unclosed\n\n- [yes]({URL})\n- `',
            f'> text\n<div>\n[hidden]({URL})\n\n[yes]({URL})',
        ]
        for text in cases:
            with self.subTest(text=text):
                self.only_source(text)
                run = self.run_report()
                self.assertEqual(run['properties']['summary']['mappedResults'], 1)
                mapped = run['results'][0]['locations'][0]['physicalLocation']['region']
                self.assertEqual(mapped['startLine'], text.split('\n').index(
                    next(line for line in text.split('\n') if '[yes]' in line)) + 1)

    def test_spaces_marker_width_and_original_destination_spelling(self):
        cases = [
            (f'   >  + [x](<{URL}>)', 14, 32),
            (f'999999999) [x]({URL})', 16, 34),
            (f'>[x]({URL})', 6, 24),
            (f'*    [x]({URL})', 10, 28),
            (f'+ [x]({URL})', 7, 25),
            (f'- text\n\n  [x]({URL})', 7, 25),
            (f'> 😀\t[x]({URL})', 9, 27),
        ]
        for text, start, end in cases:
            with self.subTest(text=text):
                self.only_source(text)
                region = self.run_report()['results'][0]['locations'][0]['physicalLocation'].get('region')
                line = text.count('\n') + 1
                self.assertEqual(region, {'startLine': line, 'startColumn': start,
                                          'endLine': line, 'endColumn': end})

    def test_decoded_destination_uses_original_container_columns(self):
        self.only_source('> - [x](https://x.invalid/a\\(b\\)?a=1&amp;b=2 "title")')
        self.report['error_map']['nested.md'][0]['url'] = 'https://x.invalid/a(b)?a=1&b=2'
        region = self.run_report()['results'][0]['locations'][0]['physicalLocation']['region']
        self.assertEqual(region, {'startLine': 1, 'startColumn': 9,
                                  'endLine': 1, 'endColumn': 45})


if __name__ == '__main__':
    unittest.main()
