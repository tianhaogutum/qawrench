"""Independent regions and literal boundaries for single-line inline <![CDATA[...]]>."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qawrench.links import convert, load_json

FIXTURE = Path(__file__).parent / 'fixtures/markdown-inline-cdata'
URL = 'https://x.invalid/'


def without_context(sarif):
    result = deepcopy(sarif)
    for r in result['runs'][0]['results']:
        r['locations'][0]['physicalLocation'].pop('contextRegion', None)
    return result


class MarkdownInlineCDATATests(unittest.TestCase):
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
                             'text <![CDATA[a`]]> [one](https://x.invalid/) <![CDATA[b`]]> [two](https://x.invalid/)\r\n'})] * 2)

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

    def test_delimiters_empty_nested_openers_quotes_and_hidden_syntax(self):
        # Only exact <![CDATA[ starts; the first literal ]]> closes. No CDATA body
        # grammar, nesting state, quote escapes or backslash escapes are added.
        for body in ['', ' ', '`', '`` [ ![ < > " \'', 'outer <![CDATA[ inner',
                     '<!-- unclosed', '<!-- outer <!-- nested -->',
                     f'[hidden]({URL}) <{URL}> [r]: {URL}', 'end\\']:
            with self.subTest(body=body):
                self.assert_count(f'[one]({URL}) <![CDATA[{body}]]> [two]({URL}) <![CDATA[`]]> <{URL}>', 3)
                self.assert_count(f'text <![CDATA[{body}]]>', 0)
        for body in ['"quoted', "'quoted", 'escaped\\']:
            self.assert_count(f'text <![CDATA[{body}]]> [one]({URL}) ]]> [two]({URL})', 2)
        self.assert_count(f'text <![CDATA[ [hidden]({URL}) > [hidden]({URL}) ]]> [one]({URL})', 1)

    def test_earlier_prose_label_and_cross_line_code_keep_precedence(self):
        for text in [f'`open <![CDATA[close` [one]({URL}) ]]> [two]({URL})',
                     f'[a` <![CDATA[close` b]({URL}) <![CDATA[hidden]]> [two]({URL})']:
            self.assert_count(text, 2)
        for prefix, continuation in [('', ''), ('> ', '> '), ('- ', '  '),
                                     ('> - ', '>   '), ('> ', ''), ('- ', '')]:
            with self.subTest(prefix=prefix, continuation=continuation):
                self.assert_count(f'{prefix}`open\n{continuation}text <![CDATA[hidden]]> [one]({URL})\n{continuation}close`', 0)
                self.assert_count(f'{prefix}text <![CDATA[a`]]> [one]({URL})\n{continuation}text <![CDATA[b`]]> [two]({URL})', 2)
        self.assert_count(f'`open\n# text <![CDATA[`]]> [one]({URL})\nclose`', 1)
        self.assert_count(f'# text <![CDATA[a`]]> [one]({URL})\n## text <![CDATA[b`]]> [two]({URL})', 2)

    def test_other_literal_tokens_keep_precedence(self):
        for literal in [f'[one]({URL} "<![CDATA[a`]]>")', f'[one]({URL} \'<![CDATA[a`]]>\')',
                        f'[one]({URL} (<![CDATA[a`]]>))',
                        '<i title="<![CDATA[a`]]> [fake]">', '<!-- <![CDATA[a`]]> [fake] -->',
                        '<? <![CDATA[a`]]> [fake] ?>']:
            with self.subTest(literal=literal):
                self.assert_count(f'text {literal} <![CDATA[b`]]> [two]({URL})',
                                  2 if literal.startswith('[one]') else 1)
        self.assert_count(f'text ![one]({URL} "<![CDATA[a`]]>") <![CDATA[b`]]> [two]({URL})', 1)
        # Valid autolink body has no nested angle; its ]]> and backtick are literal.
        self.assert_count(f'text <{URL}a`]]> <![CDATA[b`]]> [two]({URL})', 1)
        self.assert_count(f'[one](<{URL}a`>) <![CDATA[b`]]> [two]({URL})', 1)

    def test_incomplete_inexact_and_multiline_tokens_get_no_new_protection(self):
        for token in ['<![CDATA[a`>', '<![CDATA[a`] ]>', '<![cdata[a`]]>',
                      '< ![CDATA[a`]]>', '<![CDATA[a`', '<![CDATA[a`\rcontinued]]>',
                      '<![CDATA[a`\ncontinued]]>', '<![CDATA[a`\r\ncontinued]]>']:
            with self.subTest(token=token):
                # First unprotected backtick still pairs into the second token.
                self.assert_count(f'text {token} [one]({URL}) <![CDATA[b`]]> [two]({URL})', 1)
        # An escaped opener retains the old unsupported nested-label fallback,
        # which can suppress the rest of the line; it gains no CDATA protection.
        self.assert_count(f'text \\<![CDATA[a`]]> [one]({URL}) <![CDATA[b`]]> [two]({URL})', 0)

    def test_exclusions_and_no_invented_reference_definitions(self):
        pair = f'text <![CDATA[a`]]> [one]({URL}) <![CDATA[b`]]> [two]({URL})'
        for text in [f'``{pair}``', f'~~~\n{pair}\n~~~', f'    {pair}',
                     f'> ```\n> {pair}\n> ```', f'-     {pair}',
                     f'<![CDATA[closed]]> {pair}', f'> <![CDATA[closed]]> {pair}',
                     f'<![CDATA[open\ntext ` [one]({URL})\n]]> [one]({URL})',
                     f'<div>\n{pair}\n</div>', f'> <script>\n> {pair}\n> </script>',
                     f'text <![CDATA[ [r]: {URL} ]]>\n\n[r]',
                     f'- start <![CDATA[hidden]]>\n[r]: {URL}\n\n[r]',
                     f'> start <![CDATA[hidden]]>\n[r]: {URL}\n\n[r]',
                     f'text <![CDATA[ [unclosed ]]> [one]({URL} "a`") [two]({URL} "b`")',
                     f'text <![CDATA[hidden]]> ![one]({URL})']:
            with self.subTest(text=text):
                # The unclosed bracket inside the CDATA cannot corrupt later tails.
                self.assert_count(text, 2 if text.startswith('text <![CDATA[ [unclosed') else 0)

        # A block-start CDATA ends on the first literal terminator, including one
        # in the next line's apparent inline CDATA. Following lines resume prose.
        self.assert_count(f'<![CDATA[open\n{pair}\n]]> [one]({URL})', 1)

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
        self.only_source(f'text <![CDATA[{URL}]]>\r\n')
        manifest = [dict(source='basic.md', url=URL, line=1, column=15, occurrenceId='literal-cdata')]
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
