"""Source-region and exclusion contracts for bounded prose inline comments."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from qawrench.links import convert, load_json


FIXTURE = Path(__file__).parent / 'fixtures/markdown-inline-comments'
URL = 'https://x.invalid/'


class MarkdownInlineCommentTests(unittest.TestCase):
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

    def test_surrounding_links_regression(self):
        self.assertEqual(self.regions(self.run_report()),
                         load_json(FIXTURE / 'expected.json')['basic.md'])

    def test_independent_references_autolinks_containers_and_unicode(self):
        run = self.run_report()
        expected = load_json(FIXTURE / 'expected.json')
        self.assertEqual({s: self.regions(run, s) for s in expected}, expected)
        summary = run['properties']['summary']
        self.assertEqual((summary['mappedResults'], summary['unmappedFindings'], summary['resultCount']),
                         (13, 2, 15))
        self.assertEqual(len({json.dumps(r['partialFingerprints']) for r in run['results']}), 15)
        for result in run['results']:
            physical = result['locations'][0]['physicalLocation']
            if not result['properties']['mapped']:
                self.assertNotIn('region', physical)
            # Independent exact-region positional identity, including file-only.
            identity = [result['properties']['category'], result['properties']['source'],
                        result['properties']['originalEntry']['url'],
                        physical.get('region')]
            digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(',', ':'),
                                               sort_keys=True).encode()).hexdigest()
            self.assertEqual(list(result['partialFingerprints'].values()), [digest])

    def test_crlf_default_literal_manifest_and_exact_baseline(self):
        before = convert(self.report, root=self.root, markdown=True)
        for source in self.report['error_map']:
            path = self.root / source
            path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n'))
        self.assertEqual(before['runs'][0], self.run_report())
        compared = self.run_report(baseline=before)
        self.assertEqual(compared['properties']['summary']['baselineComparison'],
                         {'new': 0, 'unchanged': 15, 'updated': 0, 'moved': 0, 'absent': 0})
        self.assertEqual([r['partialFingerprints'] for r in before['runs'][0]['results']],
                         [r['partialFingerprints'] for r in compared['results']])
        default = convert(self.report, root=self.root)['runs'][0]
        self.assertEqual((default['properties']['summary']['mappedResults'], len(default['results'])), (0, 4))
        self.only_source(f'text <!-- {URL} -->')
        literal = convert(self.report, root=self.root, manifest=[
            {'source': 'basic.md', 'url': URL, 'line': 1, 'column': 11}])
        self.assertEqual(literal['runs'][0]['properties']['summary']['mappedResults'], 1)
        self.assertEqual(self.regions(self.run_report()), [])

    def test_exact_delimiters_repeated_comments_and_hidden_syntax(self):
        for content in ['', ' ', '`', '`` [ ![ < > " \'', f'[hidden]({URL}) <{URL}>',
                        f'![x]({URL}) [r]: {URL}', 'end\\']:
            with self.subTest(content=content):
                self.only_source(f'[one]({URL}) <!--{content}--> [two]({URL}) <!--`--> <{URL}>')
                self.assertEqual(len(self.regions(self.run_report())), 3)
        self.only_source(f'e\u0301😀\t[one]({URL}) <!-- ` --> [two]({URL})')
        self.assertEqual(self.regions(self.run_report()), [
            dict(startLine=1, startColumn=11, endLine=1, endColumn=29),
            dict(startLine=1, startColumn=48, endLine=1, endColumn=66)])
        # The first literal terminator closes; a backslash cannot escape it.
        self.only_source(f'text <!-- hidden\\--> [one]({URL}) --> [two]({URL})')
        self.assertEqual(len(self.regions(self.run_report())), 2)

    def test_earlier_code_and_label_precedence_and_paragraph_scope(self):
        for text in [f'`open <!-- close` [one]({URL}) --> [two]({URL})',
                     f'[a` <!-- close` b]({URL}) <!-- hidden --> [two]({URL})']:
            with self.subTest(text=text):
                self.only_source(text)
                self.assertEqual(len(self.regions(self.run_report())), 2)
        for prefix, continuation in [('', ''), ('> ', '> '), ('- ', '  '),
                                     ('> - ', '>   '), ('> ', ''), ('- ', '')]:
            with self.subTest(prefix=prefix, continuation=continuation):
                self.assert_file_only(f'{prefix}`open\n{continuation}text <!-- hidden --> [one]({URL})\n{continuation}close`')
                self.only_source(f'{prefix}text <!-- ` --> [one]({URL})\n{continuation}text <!-- ` --> [two]({URL})')
                self.assertEqual(len(self.regions(self.run_report())), 2)
        self.only_source(f'`open\n# text <!-- ` --> [one]({URL})\nclose`')
        self.assertEqual(len(self.regions(self.run_report())), 1)

    def test_literal_titles_images_autolinks_and_raw_attributes_keep_boundaries(self):
        for literal in [f'[one]({URL} "<!-- ` -->")', f'[one]({URL} \'<!-- ` -->\')',
                        f'[one]({URL} (<!-- ` -->))', f'<{URL}a`>',
                        '<i title="<!-- ` > [fake] -->">']:
            with self.subTest(literal=literal):
                self.only_source(f'text {literal} <!-- ` --> [two]({URL})')
                self.assertEqual(len(self.regions(self.run_report())),
                                 2 if literal.startswith('[one]') else 1)
        self.only_source(f'text ![one]({URL} "<!-- ` -->") <!-- ` --> [two]({URL})')
        self.assertEqual(len(self.regions(self.run_report())), 1)
        self.only_source(f'text <!-- [hidden]({URL}) --> <{URL}> <!-- ` -->')
        self.assertEqual(len(self.regions(self.run_report())), 1)

    def test_malformed_nested_and_multiline_comments_remain_conservative(self):
        for comment in ['<!-- missing', '<!-- hidden -- >', '<!-- outer <!-- inner -->',
                        '<!-- outer <!-- inner --> -->', '<!-- hidden\ncontinued -->']:
            with self.subTest(comment=comment):
                self.assert_file_only(f'[one]({URL}) {comment} [two]({URL})')
        self.assert_file_only(f'text <!-- unclosed\n[one]({URL})\n--> [two]({URL})')

    def test_block_exclusions_and_no_invented_definitions(self):
        pair = f'[one]({URL}) <!-- ` --> [two]({URL})'
        for text in [f'``{pair}``', f'~~~\n{pair}\n~~~', f'    {pair}',
                     f'> ```\n> {pair}\n> ```', f'-     {pair}',
                     f'<!-- hidden --> {pair}', f'> <!-- hidden --> {pair}',
                     f'<div>\n{pair}\n</div>', f'> <script>\n> {pair}\n> </script>',
                     f'text <!-- [r]: {URL} -->\n\n[r]',
                     f'- start <!-- hidden -->\n[r]: {URL}\n\n[r]',
                     f'> start <!-- hidden -->\n[r]: {URL}\n\n[r]',
                     f'text <!-- hidden --> ![one]({URL})',
                     f'- start\n# text <!-- hidden -->\n[r]: {URL}\n\n[r]']:
            with self.subTest(text=text):
                self.assert_file_only(text)


if __name__ == '__main__':
    unittest.main()
