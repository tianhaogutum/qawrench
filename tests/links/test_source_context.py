"""Opt-in original-source context; identity and existing evidence stay authoritative."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from qawrench.links import InputError, convert, load_json

ROOT = Path(__file__).resolve().parents[2]
URL = 'https://x.invalid/'
LINE = 'e\u0301😀\t[one](https://x.invalid/) <!-- <script>alert("x")</script> --> [two](https://x.invalid/)\r\n'
REGIONS = [dict(startLine=2, startColumn=11, endLine=2, endColumn=29),
           dict(startLine=2, startColumn=74, endLine=2, endColumn=92)]


def without_context(sarif):
    result = deepcopy(sarif)
    for r in result['runs'][0]['results']:
        for loc in r.get('locations', []):
            loc['physicalLocation'].pop('contextRegion', None)
    return result


class SourceContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'page.md'
        self.path.write_bytes(('before\n' + LINE + 'after\r\r[ref]: ' + URL + '\n\n[ref] tail').encode())
        self.report = load_json(ROOT / 'examples/links/lychee.json')
        self.report.update(total=3, errors=2, timeouts=0, successful=0, unsupported=1)
        self.report['success_map'] = {}
        self.report['error_map'] = {'page.md': [
            {'url': URL, 'status': {'text': 'Network error', 'code': 404}},
            {'url': 'https://unmatched.invalid/', 'status': {'text': 'Network error', 'code': 404}}]}
        self.manifest = [dict(source='page.md', url=URL, line=2, column=r['startColumn'],
                              occurrenceId='use-' + str(n)) for n, r in enumerate(REGIONS)]

    def convert(self, **options):
        return convert(self.report, root=self.root, **options)

    def physical(self, sarif):
        return [r['locations'][0]['physicalLocation'] for r in sarif['runs'][0]['results']
                if r['properties']['mapped']]

    def test_literal_original_unicode_crlf_line_regression(self):
        before = self.convert(manifest=self.manifest)
        after = self.convert(manifest=self.manifest, source_context=True)
        self.assertEqual(without_context(after), before)
        physical = self.physical(after)
        self.assertEqual([p['region'] for p in physical], REGIONS)
        self.assertEqual([p['contextRegion'] for p in physical], [
            dict(startLine=2, endLine=2, snippet={'text': LINE})] * 2)
        for r in after['runs'][0]['results']:
            if not r['properties']['mapped']:
                for loc in r.get('locations', []):
                    self.assertNotIn('region', loc['physicalLocation'])
                    self.assertNotIn('contextRegion', loc['physicalLocation'])
        self.assertEqual(json.loads(json.dumps(after, ensure_ascii=False)), after)

    def test_markdown_inline_and_reference_context_without_final_newline(self):
        sarif = self.convert(markdown=True, source_context=True)
        plain = self.convert(markdown=True)
        self.assertEqual(without_context(sarif), plain)
        physical = self.physical(sarif)
        self.assertEqual([p['region'] for p in physical[:2]], REGIONS)
        self.assertEqual([p['contextRegion']['snippet']['text'] for p in physical],
                         [LINE, LINE, '[ref] tail'])
        self.assertEqual(physical[-1]['contextRegion'],
                         dict(startLine=7, endLine=7, snippet={'text': '[ref] tail'}))

    def test_no_mapping_does_not_invent_context_and_default_false_is_identical(self):
        self.assertEqual(self.convert(source_context=True), self.convert())
        for opts in [{}, {'manifest': self.manifest}, {'markdown': True}]:
            self.assertEqual(json.dumps(self.convert(**opts), ensure_ascii=False),
                             json.dumps(self.convert(**opts, source_context=False), ensure_ascii=False))
        for bad in [None, 1, 'yes', []]:
            with self.subTest(value=bad), self.assertRaisesRegex(InputError, 'source_context.*boolean'):
                self.convert(source_context=bad)

    def test_reuses_single_validated_snapshot_even_if_file_changes(self):
        read_bytes = Path.read_bytes
        reads = []
        def read_then_change(path):
            data = read_bytes(path)
            reads.append(path)
            path.write_bytes(b'replaced after read')
            return data
        with patch.object(Path, 'read_bytes', read_then_change):
            after = self.convert(manifest=self.manifest, source_context=True)
        self.assertEqual(reads, [self.path])
        self.assertEqual([p['contextRegion']['snippet']['text'] for p in self.physical(after)], [LINE, LINE])

    def test_line_separators_and_exact_complete_line_boundaries(self):
        for newline in ['\n', '\r\n', '\r', '']:
            with self.subTest(newline=newline):
                line = '😀\t\u2028\u0085\v\f' + URL + ' suffix' + newline
                self.path.write_bytes(line.encode())
                sarif = self.convert(manifest=[dict(source='page.md', url=URL, line=1, column=7)],
                                     source_context=True)
                self.assertEqual(self.physical(sarif)[0]['contextRegion'],
                                 dict(startLine=1, endLine=1, snippet={'text': line}))

    def test_multiline_mapped_reference_region_context_boundary(self):
        # The current Markdown parser only emits single-line regions. Exercise
        # context plumbing with a supplied future multiline reference-use region.
        text = 'before\r\n😀 [label]\r\n[ref] tail\nnext\n'
        self.path.write_bytes(text.encode())
        for end_line, end_column in [(3, 6), (4, 1)]:
            with self.subTest(end=(end_line, end_column)):
                region = dict(startLine=2, startColumn=3, endLine=end_line, endColumn=end_column)
                with patch('qawrench.links.adapter.link_occurrences', return_value=iter([(URL, region)])):
                    sarif = self.convert(markdown=True, source_context=True)
                self.assertEqual(self.physical(sarif), [{
                    'artifactLocation': {'uri': 'page.md', 'uriBaseId': '%SRCROOT%'},
                    'region': region,
                    'contextRegion': dict(startLine=2, endLine=3, snippet={'text': '😀 [label]\r\n[ref] tail\n'})}])

    def test_context_is_ignored_in_baselines_in_both_directions_and_after_source_edit(self):
        for mapping in [{'manifest': self.manifest}, {'markdown': True}]:
            for before_context in [False, True]:
                before = self.convert(**mapping, source_context=before_context)
                for after_context in [False, True]:
                    compared = self.convert(**mapping, source_context=after_context, baseline=before)
                    counts = compared['runs'][0]['properties']['summary']['baselineComparison']
                    self.assertEqual(counts, dict(new=0, unchanged=len(before['runs'][0]['results']),
                                                 updated=0, moved=0, absent=0))
                    # Compared contextual reports can themselves be baselines.
                    self.convert(**mapping, baseline=compared)
        before = self.convert(manifest=self.manifest, source_context=True)
        self.path.write_bytes(self.path.read_bytes().replace(b'alert("x")', b'alert("different")'))
        after = self.convert(manifest=[self.manifest[0]], source_context=True, baseline=before)
        counts = after['runs'][0]['properties']['summary']['baselineComparison']
        self.assertEqual(counts, dict(new=0, unchanged=3, updated=0, moved=0, absent=1))

    def test_baseline_context_must_be_structurally_valid_and_mapped(self):
        before = self.convert(manifest=self.manifest, source_context=True)
        for mutate in [lambda c: c.update(startLine=1), lambda c: c.update(endLine=True),
                       lambda c: c['snippet'].update(text='wrong line count\nextra\n'),
                       lambda c: c['snippet'].update(text=42),
                       lambda c: c['snippet'].update(markdown='do not render'),
                       lambda c: c.update(startColumn=1)]:
            bad = deepcopy(before)
            mutate(self.physical(bad)[0]['contextRegion'])
            with self.subTest(mutate=mutate), self.assertRaises(InputError):
                self.convert(manifest=self.manifest, baseline=bad)
        bad = deepcopy(before)
        unmapped = next(r for r in bad['runs'][0]['results'] if r['properties'].get('source') and not r['properties']['mapped'])
        unmapped['locations'][0]['physicalLocation']['contextRegion'] = self.physical(before)[0]['contextRegion']
        with self.assertRaises(InputError):
            self.convert(manifest=self.manifest, baseline=bad)

    def test_moved_new_and_diagnostic_updates_are_identical_with_context(self):
        before = self.convert(manifest=self.manifest[:1], source_context=True)
        self.path.write_bytes(b'inserted\n' + self.path.read_bytes())
        moved = [dict(m, line=3) for m in self.manifest]
        self.report['error_map']['page.md'][0]['status']['code'] = 500
        plain = self.convert(manifest=moved, baseline=before)
        contextual = self.convert(manifest=moved, baseline=before, source_context=True)
        self.assertEqual(without_context(contextual), plain)
        self.assertEqual(plain['runs'][0]['properties']['summary']['baselineComparison'],
                         dict(new=1, unchanged=2, updated=1, moved=1, absent=0))

    def test_source_errors_and_invalid_occurrences_remain_errors(self):
        for data in [b'\xff', None]:
            with self.subTest(data=data):
                if data is None:
                    self.path.unlink()
                else:
                    self.path.write_bytes(data)
                with self.assertRaises(InputError):
                    self.convert(source_context=True)
        self.path.write_bytes(LINE.encode())
        with self.assertRaisesRegex(InputError, 'URL mismatch'):
            self.convert(manifest=self.manifest, source_context=True)
        outside = self.root / 'outside.md'
        outside.symlink_to('/etc/passwd')
        self.report['error_map'] = {'outside.md': self.report['error_map']['page.md']}
        with self.assertRaisesRegex(InputError, 'symlink escape'):
            self.convert(source_context=True)

    def test_cli_json_data_gates_dry_run_and_atomic_error(self):
        report = self.root / 'report.json'
        manifest = self.root / 'manifest.json'
        output = self.root / 'out.sarif'
        baseline = self.root / 'before.sarif'
        report.write_text(json.dumps(self.report), encoding='utf-8')
        manifest.write_text(json.dumps(self.manifest), encoding='utf-8')
        base = [sys.executable, '-m', 'qawrench.links', str(report), '--root', str(self.root)]
        def cli(*args):
            return subprocess.run(base + list(args), cwd=ROOT, text=True, capture_output=True)
        for args in [['--manifest', str(manifest)], ['--markdown']]:
            result = cli(*args, '--out', str(baseline))
            self.assertEqual(result.returncode, 0, result.stderr)
            result = cli(*args, '--source-context', '--baseline', str(baseline), '--fail-on-new', '--out', str(output))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(LINE, [p['contextRegion']['snippet']['text'] for p in self.physical(load_json(output))])
            result = cli(*args, '--source-context', '--baseline', str(output), '--fail-on-new', '--dry-run')
            self.assertEqual((result.returncode, result.stdout), (0, ''), result.stderr)
        result = cli('--source-context', '--out', str(output), '--fail-on-findings')
        self.assertEqual(result.returncode, 1, result.stderr)
        sentinel = output.read_bytes()
        self.path.write_bytes(b'\xff')
        result = cli('--source-context', '--out', str(output))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(output.read_bytes(), sentinel)


if __name__ == '__main__':
    unittest.main()
