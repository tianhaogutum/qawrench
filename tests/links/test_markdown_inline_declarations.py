"""Independent regions and literal boundaries for single-line inline <!A...> declarations."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qawrench.links import convert, load_json

FIXTURE = Path(__file__).parent / 'fixtures/markdown-inline-declarations'
URL = 'https://x.invalid/'


def without_context(sarif):
    result = deepcopy(sarif)
    for r in result['runs'][0]['results']:
        r['locations'][0]['physicalLocation'].pop('contextRegion', None)
    return result


class MarkdownInlineDeclarationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report = load_json(FIXTURE / 'lychee.json')
        for source in self.report['error_map']:
            (self.root / source).write_bytes((FIXTURE / source).read_bytes())

    def sarif(self, **options):
        return convert(self.report, root=self.root, markdown=True, **options)

    def mapped(self, sarif):
        return [r for r in sarif['runs'][0]['results'] if r['properties']['mapped']]

    def only_source(self, text):
        self.report.update(total=1, errors=1)
        self.report['error_map'] = {'basic.md': self.report['error_map']['basic.md'][:1]}
        (self.root / 'basic.md').write_bytes(text.encode())

    def assert_count(self, text, count):
        self.only_source(text)
        sarif = self.sarif(source_context=True)
        self.assertEqual(len(self.mapped(sarif)), count)
        if count == 0:
            physical = sarif['runs'][0]['results'][0]['locations'][0]['physicalLocation']
            self.assertNotIn('region', physical)
            self.assertNotIn('contextRegion', physical)

    def test_backticks_regression_exact_regions_and_crlf_context(self):
        expected = load_json(FIXTURE / 'expected.json')['basic.md']
        results = [r for r in self.mapped(self.sarif(source_context=True))
                   if r['properties']['source'] == 'basic.md']
        self.assertEqual([r['locations'][0]['physicalLocation']['region'] for r in results], expected)
        self.assertEqual([r['locations'][0]['physicalLocation']['contextRegion'] for r in results],
                         [dict(startLine=1, endLine=1, snippet={'text':
                             'text <!A a`> [one](https://x.invalid/) <!B b`> [two](https://x.invalid/)\r\n'})] * 2)

    def test_independent_unicode_references_autolinks_and_scopes(self):
        sarif = self.sarif(source_context=True)
        run = sarif['runs'][0]
        expected = load_json(FIXTURE / 'expected.json')
        for source, regions in expected.items():
            results = [r for r in self.mapped(sarif) if r['properties']['source'] == source]
            self.assertEqual([r['locations'][0]['physicalLocation']['region'] for r in results], regions)
            lines = (self.root / source).read_bytes().decode().splitlines(keepends=True)
            for r, region in zip(results, regions):
                self.assertEqual(r['locations'][0]['physicalLocation']['contextRegion'],
                                 dict(startLine=region['startLine'], endLine=region['endLine'],
                                      snippet={'text': lines[region['startLine'] - 1]}))
        self.assertEqual((run['properties']['summary']['mappedResults'],
                          run['properties']['summary']['unmappedFindings'], len(run['results'])), (11, 2, 13))
        for r in run['results']:
            physical = r['locations'][0]['physicalLocation']
            if not r['properties']['mapped']:
                self.assertNotIn('region', physical)
                self.assertNotIn('contextRegion', physical)
            identity = [r['properties']['category'], r['properties']['source'],
                        r['properties']['originalEntry']['url'], physical.get('region')]
            digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                                               separators=(',', ':')).encode()).hexdigest()
            self.assertEqual(r['partialFingerprints'], {'lycheeSourceOccurrence/v1': digest})

    def test_delimiters_empty_tail_quotes_backslashes_and_hidden_syntax(self):
        # First literal > closes even in quotes or after a backslash. The
        # required ASCII uppercase opener letter is not an optional body.
        for token in ['<!A>', '<!Z>', '<!A >', '<!DOCTYPE html>', '<!ABC123>',
                      '<!A`>', '<!A `` [ ![ " \'>', '<!A outer <!B inner>',
                      '<!A <!-- unclosed>', '<!A <!-- nested <!-->',
                      f'<!A <{URL}>', f'<!A [hidden]({URL}) [r]: {URL}>', '<!A end\\>']:
            with self.subTest(token=token):
                self.assert_count(f'[one]({URL}) {token} [two]({URL}) <!B `> <{URL}>', 3)
                self.assert_count(f'text {token}', 0)
        for body in ['"quoted', "'quoted", 'escaped\\']:
            self.assert_count(f'text <!A {body}> [one]({URL}) > [two]({URL})', 2)
        # A nested < does not delay closure, nor does a quote hide later links.
        self.assert_count(f'text <!A " < [hidden]({URL})> [one]({URL})', 1)

    def test_earlier_prose_label_and_cross_line_code_keep_precedence(self):
        for text in [f'`open <!A close` [one]({URL}) > [two]({URL})',
                     f'[a` <!A close` b]({URL}) <!A hidden> [two]({URL})']:
            self.assert_count(text, 2)
        for prefix, continuation in [('', ''), ('> ', '> '), ('- ', '  '),
                                     ('> - ', '>   '), ('> ', ''), ('- ', '')]:
            with self.subTest(prefix=prefix, continuation=continuation):
                self.assert_count(f'{prefix}`open\n{continuation}text <!A hidden> [one]({URL})\n{continuation}close`', 0)
                self.assert_count(f'{prefix}text <!A a`> [one]({URL})\n{continuation}text <!A b`> [two]({URL})', 2)
        self.assert_count(f'`open\n# text <!A `> [one]({URL})\nclose`', 1)
        self.assert_count(f'# text <!A a`> [one]({URL})\n## text <!A b`> [two]({URL})', 2)

    def test_other_literal_tokens_keep_precedence(self):
        for literal in [f'[one]({URL} "<!A a`>")', f'[one]({URL} \'<!A a`>\')',
                        f'[one]({URL} (<!A a`>))',
                        '<i title="<!A a`> [fake]">', '<!-- <!A a`> [fake] -->',
                        '<? <!A a`> [fake] ?>', '<![CDATA[<!A a`> [fake]]]>']:
            with self.subTest(literal=literal):
                self.assert_count(f'text {literal} <!A b`> [two]({URL})',
                                  2 if literal.startswith('[one]') else 1)
        self.assert_count(f'text ![one]({URL} "<!A a`>") <!A b`> [two]({URL})', 1)
        # Valid autolinks retain literal backticks; they have no nested angle.
        self.assert_count(f'text <{URL}a`> <!A b`> [two]({URL})', 1)
        self.assert_count(f'[one](<{URL}a`>) <!A b`> [two]({URL})', 1)

    def test_incomplete_inexact_and_multiline_tokens_get_no_new_protection(self):
        for token in ['<!a a`>', '<!À a`>', '<!aA a`>', '<! A a`>', '< !A a`>',
                      '<!1 a`>', '<!A a`', '<!A a`\rcontinued>',
                      '<!A a`\ncontinued>', '<!A a`\r\ncontinued>',
                      '\\<!A a`>']:
            with self.subTest(token=token):
                # Unprotected backtick still pairs into the second declaration.
                self.assert_count(f'text {token} [one]({URL}) <!B b`> [two]({URL})', 1)
        self.assert_count(f'text <!> [one]({URL})', 1)

    def test_exclusions_and_no_invented_reference_definitions(self):
        pair = f'text <!A a`> [one]({URL}) <!A b`> [two]({URL})'
        for text in [f'``{pair}``', f'~~~\n{pair}\n~~~', f'    {pair}',
                     f'> ```\n> {pair}\n> ```', f'-     {pair}',
                     f'<!A closed> {pair}', f'> <!A closed> {pair}',
                     f'<!A open\ntext ` [one]({URL})\nend> [one]({URL})',
                     f'<div>\n{pair}\n</div>', f'> <script>\n> {pair}\n> </script>',
                     f'text <!A  [r]: {URL} >\n\n[r]',
                     f'- start <!A hidden>\n[r]: {URL}\n\n[r]',
                     f'> start <!A hidden>\n[r]: {URL}\n\n[r]',
                     f'text <!A  [unclosed > [one]({URL} "a`") [two]({URL} "b`")',
                     f'text <!A hidden> ![one]({URL})']:
            with self.subTest(text=text):
                # The unclosed bracket inside the declaration cannot corrupt later tails.
                self.assert_count(text, 2 if text.startswith('text <!A  [unclosed') else 0)

        # A block-start declaration ends on the first literal terminator, including one
        # in the next line's apparent inline declaration. Following lines resume prose.
        self.assert_count(f'<!A open\n{pair}\nend> [one]({URL})', 1)

    def test_context_snapshot_identity_baselines_default_and_literal_modes(self):
        plain = self.sarif()
        contextual = self.sarif(source_context=True)
        self.assertEqual(without_context(contextual), plain)
        for before in [plain, contextual]:
            for context in [False, True]:
                after = self.sarif(baseline=before, source_context=context)
                self.assertEqual(after['runs'][0]['properties']['summary']['baselineComparison'],
                                 dict(new=0, unchanged=13, updated=0, moved=0, absent=0))
        self.assertEqual(convert(self.report, root=self.root, source_context=True),
                         convert(self.report, root=self.root))
        self.only_source(f'text <!A {URL}>\r\n')
        manifest = [dict(source='basic.md', url=URL, line=1, column=10, occurrenceId='literal-declaration')]
        plain = convert(self.report, root=self.root, manifest=manifest)
        contextual = convert(self.report, root=self.root, manifest=manifest, source_context=True, baseline=plain)
        self.assertEqual(contextual['runs'][0]['results'][0]['partialFingerprints'],
                         plain['runs'][0]['results'][0]['partialFingerprints'])
        self.assertEqual(len(self.mapped(self.sarif())), 0)
        self.only_source((FIXTURE / 'basic.md').read_bytes().decode())
        read_bytes = Path.read_bytes
        reads = []
        def read_then_replace(path):
            data = read_bytes(path)
            reads.append(path)
            path.write_bytes(b'replaced')
            return data
        with patch.object(Path, 'read_bytes', read_then_replace):
            after = self.sarif(source_context=True)
        self.assertEqual(reads, [self.root / 'basic.md'])
        self.assertEqual(after['runs'][0]['results'][0]['locations'][0]['physicalLocation']['contextRegion']['snippet']['text'],
                         (FIXTURE / 'basic.md').read_bytes().decode())


if __name__ == '__main__':
    unittest.main()
