"""Independent source regions and negative contracts for inline HTML boundaries."""

import json
from pathlib import Path
import tempfile
import unittest

from qawrench.links import convert, load_json


FIXTURE = Path(__file__).parent / 'fixtures/markdown-html-boundaries'
URL = 'https://x.invalid/'


class MarkdownHtmlBoundaryTests(unittest.TestCase):
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

    def test_attribute_backticks_regression(self):
        self.assertEqual(self.regions(self.run_report()),
                         load_json(FIXTURE / 'expected.json')['basic.md'])

    def test_independent_references_containers_heading_and_unicode_regions(self):
        run = self.run_report()
        expected = load_json(FIXTURE / 'expected.json')
        self.assertEqual({s: self.regions(run, s) for s in expected}, expected)
        summary = run['properties']['summary']
        self.assertEqual((summary['mappedResults'], summary['unmappedFindings'], summary['resultCount']),
                         (10, 2, 12))
        self.assertEqual(len({json.dumps(r['partialFingerprints']) for r in run['results']}), 12)
        for result in run['results']:
            if not result['properties']['mapped']:
                self.assertNotIn('region', result['locations'][0]['physicalLocation'])

    def test_crlf_default_manifest_and_baseline_contracts(self):
        before = convert(self.report, root=self.root, markdown=True)
        for source in self.report['error_map']:
            path = self.root / source
            path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n'))
        self.assertEqual(before['runs'][0], self.run_report())
        self.assertEqual(self.run_report(baseline=before)['properties']['summary']['baselineComparison'],
                         {'new': 0, 'unchanged': 12, 'updated': 0, 'moved': 0, 'absent': 0})
        default = convert(self.report, root=self.root)['runs'][0]
        self.assertEqual((default['properties']['summary']['mappedResults'], len(default['results'])), (0, 4))
        # Explicit literal evidence retains its own contract, even inside HTML.
        self.only_source(f'text <i title="{URL}">')
        literal = convert(self.report, root=self.root, manifest=[
            {'source': 'basic.md', 'url': URL, 'line': 1, 'column': 16}])
        self.assertEqual(literal['runs'][0]['properties']['summary']['mappedResults'], 1)
        self.assertEqual(self.regions(self.run_report()), [])

    def test_supported_single_line_tag_and_attribute_forms(self):
        for tag in ['<i title="a`">', "<i title='a`'>", '<I data-x="``a" />',
                    '<x-widget _a="a`" ns:b=word disabled>', '<i title="a`>b">',
                    "<i title='a`>b'>", '<i title="a`\'b">', '<i title="a`\\">',
                    '<i\tdata-x = "a`"\t/>']:
            with self.subTest(tag=tag):
                self.only_source(f'text {tag} [one]({URL}) {tag} [two]({URL})')
                self.assertEqual(len(self.regions(self.run_report())), 2)
        # HTML has no backslash quote escapes; the final quote above closes it.
        self.only_source(f'e\u0301😀\t<i title="a`"> [one]({URL}) <i title="b`"> [two]({URL})')
        self.assertEqual(self.regions(self.run_report()), [
            dict(startLine=1, startColumn=26, endLine=1, endColumn=44),
            dict(startLine=1, startColumn=67, endLine=1, endColumn=85)])

    def test_quoted_angles_brackets_and_quotes_do_not_leak_or_change_label_state(self):
        for value in [f'a`> [fake]({URL})', f'a`> <{URL}>', f"a`\' [fake]({URL})"]:
            with self.subTest(value=value):
                self.only_source(f'text <i title="{value}"> [one]({URL}) <i title="b`"> [two]({URL})')
                self.assertEqual(len(self.regions(self.run_report())), 2)
                self.assert_file_only(f'text <i title="{value}">')
        self.only_source(f'text <i title="[`"> [one]({URL} "a`") [two]({URL} "b`")')
        self.assertEqual(len(self.regions(self.run_report())), 2)
        self.only_source(f'text <i title="a`"> [one]({URL}) </i> [two]({URL}) `code`')
        self.assertEqual(len(self.regions(self.run_report())), 2)

    def test_invalid_or_multiline_tags_do_not_protect_prose_backticks(self):
        for tag in ['<i title="a`>', '<i title=a`>', '<i title="a`" extra=>',
                    '<i title="a`"', '<i title="a`\ncontinued">', '<i\ntitle="a`">',
                    '<3 title="a`">', '<i title="a`\\"quoted">']:
            with self.subTest(tag=tag):
                self.only_source(f'text {tag} [one]({URL}) <i title="b`"> [two]({URL})')
                # The first unprotected backtick pairs into the second tag.
                # With an unquoted malformed value, masking also removes its
                # close angle; the existing conservative angle skip consumes
                # the rest of the line instead of inventing a mapping.
                self.assertEqual(len(self.regions(self.run_report())),
                                 0 if tag == '<i title=a`>' else 1)

    def test_earlier_prose_label_and_cross_line_code_keep_precedence(self):
        for text in [f'`text <i title="a`"> [one]({URL})',
                     f'[a`b <i title="a`">]({URL})']:
            with self.subTest(text=text):
                self.only_source(text)
                # Earlier code closes in the attribute, then ordinary parsing resumes.
                self.assertEqual(len(self.regions(self.run_report())), 1)
        for prefix, continuation in [('', ''), ('> ', '> '), ('- ', '  '),
                                     ('> - ', '>   '), ('> ', ''), ('- ', '')]:
            with self.subTest(prefix=prefix, continuation=continuation):
                self.assert_file_only(f'{prefix}`open\n{continuation}text <i title="a"> [one]({URL})\n{continuation}close`')
                self.only_source(f'{prefix}text <i title="a`"> [one]({URL})\n{continuation}text <i title="b`"> [two]({URL})')
                self.assertEqual(len(self.regions(self.run_report())), 2)
        self.assert_file_only(f'``text <i title="a`"> [one]({URL})``')

    def test_exclusions_and_no_invented_reference_definitions(self):
        pair = f'text <i title="a`"> [one]({URL}) <i title="b`"> [two]({URL})'
        for text in [f'``{pair}``', f'~~~\n{pair}\n~~~', f'    {pair}',
                     f'> ```\n> {pair}\n> ```', f'-     {pair}', f'<!-- {pair} -->',
                     f'<div>\n{pair}\n</div>', f'> <script>\n> {pair}\n> </script>',
                     f'text <i title="a`> [{URL}]({URL})">',
                     f'text <i title="a`"> ![one]({URL}) <i title="b`"> ![two]({URL})',
                     f'- start\n[r]: {URL}\n\ntext <i title="a`"> [r] <i title="b`"> [r]',
                     f'> start\n[r]: {URL}\n\ntext <i title="a`"> [r] <i title="b`"> [r]']:
            with self.subTest(text=text):
                self.assert_file_only(text)


if __name__ == '__main__':
    unittest.main()
