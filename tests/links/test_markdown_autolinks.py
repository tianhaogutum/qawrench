"""Bounded HTTP(S) angle tokens with independently authored source regions."""

import json
from pathlib import Path
import tempfile
import unittest

from qawrench.links import InputError, convert, load_json
from qawrench.links.markdown import link_occurrences


FIXTURE = Path(__file__).parent / 'fixtures/markdown-autolinks'
URL = 'https://x.invalid/'


class MarkdownAutolinkTests(unittest.TestCase):
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

    def only_source(self, text, urls=(URL,)):
        self.report.update(total=len(urls), errors=len(urls))
        self.report['error_map'] = {'basic.md': [
            {'url': url, 'status': {'text': 'Network error', 'code': 404}} for url in urls]}
        (self.root / 'basic.md').write_text(text, encoding='utf-8')

    def assert_file_only(self, text, urls=(URL,)):
        self.only_source(text, urls)
        run = self.run_report()
        self.assertEqual(run['properties']['summary']['mappedResults'], 0)
        self.assertEqual(len(run['results']), len(urls))
        for result in run['results']:
            self.assertNotIn('region', result['locations'][0]['physicalLocation'])

    def test_repeated_angle_autolinks_regression(self):
        self.assertEqual(self.regions(self.run_report()),
                         load_json(FIXTURE / 'expected.json')['basic.md'])

    def test_independent_container_and_unicode_regions(self):
        expected = load_json(FIXTURE / 'expected.json')
        run = self.run_report()
        self.assertEqual({s: self.regions(run, s) for s in expected}, expected)
        summary = run['properties']['summary']
        self.assertEqual((summary['mappedResults'], summary['unmappedFindings'], summary['resultCount']),
                         (9, 2, 11))
        self.assertEqual(len({json.dumps(r['partialFingerprints']) for r in run['results']}), 11)

    def test_crlf_default_manifest_and_baseline_contracts(self):
        before = convert(self.report, root=self.root, markdown=True)
        for source in self.report['error_map']:
            path = self.root / source
            path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n'))
        self.assertEqual(before['runs'][0], self.run_report())
        self.assertEqual(self.run_report(baseline=before)['properties']['summary']['baselineComparison'],
                         {'new': 0, 'unchanged': 11, 'updated': 0, 'moved': 0, 'absent': 0})
        default = convert(self.report, root=self.root)['runs'][0]
        self.assertEqual((default['properties']['summary']['mappedResults'], len(default['results'])), (0, 4))
        # Literal manifest mode can intentionally cite a URL in code; semantic
        # exclusions apply only to opt-in Markdown mode.
        self.only_source(f'`<{URL}>`')
        self.assertEqual(self.regions(self.run_report()), [])
        literal = convert(self.report, root=self.root, manifest=[
            {'source': 'basic.md', 'url': URL, 'line': 1, 'column': 3}])
        self.assertEqual(literal['runs'][0]['properties']['summary']['mappedResults'], 1)
        with self.assertRaises(InputError):
            self.run_report(manifest=[])

    def test_exact_spelling_without_decoding_normalization_or_punctuation_trimming(self):
        for url in ['HTTP://EXAMPLE.invalid/%28x%29?x=1&amp;y=2.,()',
                    'hTtPs://x.invalid/a(b)?x=1&y=2.',
                    r'https://x.invalid/a\(b\)',
                    'https://x.invalid/é?x=&#38;',
                    'https://x.invalid/a`[x]"\'.,()']:
            with self.subTest(url=url):
                self.only_source(f'e\u0301😀\t<{url}>', (url, URL))
                run = self.run_report()
                mapped = [r for r in run['results'] if r['properties']['mapped']]
                self.assertEqual(len(mapped), 1)
                self.assertEqual(mapped[0]['properties']['originalEntry']['url'], url)
                self.assertEqual(self.regions(run), [dict(startLine=1, startColumn=6,
                                                         endLine=1, endColumn=6 + len(url))])
        for source_url, report_url in [('https://x.invalid/?a=1&amp;b=2', 'https://x.invalid/?a=1&b=2'),
                                       ('HTTPS://X.invalid/%28x%29', 'https://x.invalid/(x)'),
                                       (r'https://x.invalid/a\(b\)', 'https://x.invalid/a(b)')]:
            with self.subTest(source_url=source_url):
                self.assert_file_only(f'<{source_url}>', (report_url,))

    def test_line_start_explicit_and_lazy_container_tokens(self):
        for prefix in ['', '   ', '> ', '- ', '+ ', '* ', '1. ', '123456789) ', '> - ', '- > ']:
            with self.subTest(prefix=prefix):
                self.only_source(f'{prefix}<{URL}>')
                self.assertEqual(self.regions(self.run_report()), [
                    dict(startLine=1, startColumn=len(prefix) + 2,
                         endLine=1, endColumn=len(prefix) + 20)])
        for start, continued in [('> start', ''), ('- start', ''), ('> - start', '> '),
                                 ('- > start', '  '), ('> > start', '> ')]:
            with self.subTest(start=start, continued=continued):
                self.only_source(f'{start}\n{continued}<{URL}>')
                self.assertEqual(self.regions(self.run_report()), [
                    dict(startLine=2, startColumn=len(continued) + 2,
                         endLine=2, endColumn=len(continued) + 20)])

    def test_invalid_angle_forms_never_invent_occurrences(self):
        forms = [URL, f'<{URL}', f'<{URL}\n>', f'<{URL}\r\n>', f'<<{URL}>>',
                 f'<{URL}<{URL}>>', f'<{URL} extra>', f'<{URL}\t>',
                 '<http://>', '<https://>', '<https:/x.invalid/>', '<ftp://x.invalid/>',
                 '<mailto:a@x.invalid>', '<a@x.invalid>', '<httpſ://x.invalid/>',
                 f'\\<{URL}>']
        for text in forms:
            with self.subTest(text=text):
                self.assert_file_only(text)
        for char in [' ', '\t', '\n', '\r', '\x00', '\x1f', '\x7f', '\x80', '\x9f',
                     '\u00a0', '\u2003', '\u2028', '\u2029']:
            url = URL + 'a' + char + 'b'
            with self.subTest(char=repr(char)):
                text = f'text <{url}>'
                self.assertEqual(list(link_occurrences(text)), [])
                if char in '\n\r\x00':
                    # Strict report validation rejects these URLs before mapping.
                    self.only_source(text, (url,))
                    with self.assertRaisesRegex(InputError, 'single-line non-NUL'):
                        self.run_report()
                else:
                    self.assert_file_only(text, (url,))

    def test_code_html_comments_images_and_definitions_stay_excluded(self):
        token = f'<{URL}>'
        for text in [f'`{token}`', f'``{token}``', f'`open\n{token}\nclose`',
                     f'~~~\n{token}\n~~~', f'    {token}', f'\t{token}',
                     f'> ```\n> {token}\n> ```', f'-     {token}',
                     f'>     {token}', f'<!-- {token} -->', f'<div>\n{token}\n</div>',
                     f'<script>\n\n{token}\n</script>', f'> <pre>\n> {token}\n> </pre>',
                     f'text <i title="{token}">', f'text <img src="{URL}">',
                     f'![{token}](https://other.invalid/)', f'![image]({token})',
                     f'![image](https://other.invalid/ "{token}")', f'![{token}][missing]',
                     f'[unused]: {token}']:
            with self.subTest(text=text):
                self.assert_file_only(text)

    def test_autolinks_are_atomic_but_prior_code_spans_keep_precedence(self):
        self.only_source(f'<{URL}a`[x]> <{URL}> `{URL}`')
        self.assertEqual(self.regions(self.run_report()), [
            dict(startLine=1, startColumn=28, endLine=1, endColumn=46)])
        self.assert_file_only(f'`<{URL}a`>')
        for prefix, continuation in [('', ''), ('> ', '> '), ('- ', '  '), ('> ', ''), ('- ', '')]:
            with self.subTest(prefix=prefix, continuation=continuation):
                self.assert_file_only(f'{prefix}`open\n{continuation}<{URL}>\n{continuation}close`')
        # Keep the prior round's destination/title backtick protection.
        self.only_source(f'[one]({URL} "a`") <{URL}> [two]({URL} "b`")')
        self.assertEqual(len(self.regions(self.run_report())), 3)


if __name__ == '__main__':
    unittest.main()
