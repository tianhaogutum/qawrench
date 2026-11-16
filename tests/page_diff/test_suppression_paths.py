"""Original suppression locations on synthetic parsed DOMs, before normalization."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from qawrench.page_diff import compare_files, compare_html, load_rules
from qawrench.page_diff.cli import markdown

EXAMPLES = Path(__file__).resolve().parents[2] / 'examples' / 'page_diff' / 'suppression-paths'
BASE = {'version': 1, 'required_region': 'main'}


class SuppressionPathTests(unittest.TestCase):
    def test_reordered_identity_suppressions_have_independent_original_locations(self):
        report = compare_files(EXAMPLES / 'before.html', EXAMPLES / 'after.html',
                               load_rules(EXAMPLES / 'rules.json'))
        expected = {
            ('ignore_selectors', '<aside class="clock">10:00</aside>'): (
                '<aside class="clock">10:01</aside>', '/region/article[1]/aside[0]', '/region/article[2]/aside[0]'),
            ('ignore_selectors', '<aside class="clock">20:00</aside>'): (
                '<aside class="clock">20:01</aside>', '/region/article[2]/aside[0]', '/region/article[1]/aside[0]'),
            ('volatile_attributes', 'a'): ('d', '/region/article[1]/@data-render', '/region/article[2]/@data-render'),
            ('volatile_attributes', 'b'): ('c', '/region/article[2]/@data-render', '/region/article[1]/@data-render'),
            ('normalize_whitespace', '  $20 '): ('$20', '/region/article[2]/p[0]/#text[0]', '/region/article[1]/p[0]/#text[0]'),
        }
        self.assertEqual(len(report['suppressed']), len(expected))
        for finding in report['suppressed']:
            self.assertEqual((finding['new'], finding.get('before_path'), finding.get('after_path')),
                             expected[(finding['rule'], finding['old'])])
        self.assertEqual(sorted(c['kind'] for c in report['changes']), ['changed', 'moved', 'moved'])
        edit = next(c for c in report['changes'] if c['kind'] == 'changed')
        self.assertEqual((edit['old'], edit['new'], edit['before_path'], edit['after_path']),
                         ('$10', '$12', '/region/article[1]/p[0]/#text[0]', '/region/article[2]/p[0]/#text[0]'))

    def test_absent_ignored_node_and_whitespace_observation_have_null_location(self):
        for noise, rule, location in [
            ('<aside>noise</aside>', {'ignore_selectors': [{'selector': 'aside', 'min_matches': 0}]}, '/region/aside[0]'),
            (' \n ', {'normalize_whitespace': True}, '/region/#text[0]'),
        ]:
            for reverse in (False, True):
                with self.subTest(noise=noise, reverse=reverse):
                    before = '<main><p>Keep</p></main>'
                    after = f'<main>{noise}<p>Keep</p></main>'
                    if reverse:
                        before, after = after, before
                    report = compare_html(before, after, {**BASE, **rule})
                    self.assertEqual(report['changes'], [])
                    self.assertEqual(len(report['suppressed']), 1)
                    finding = report['suppressed'][0]
                    self.assertIn('before_path', finding)
                    self.assertIn('after_path', finding)
                    self.assertEqual((finding['before_path'], finding['after_path']),
                                     (location, None) if reverse else (None, location))

    def test_missing_volatile_attribute_is_null_but_empty_attribute_exists(self):
        rules = {**BASE, 'volatile_attributes': [{'selector': 'p', 'attributes': ['title']}]}
        for old, new, locations in [
            ('', ' title="new"', (None, '/region/p[0]/@title')),
            (' title="old"', '', ('/region/p[0]/@title', None)),
            (' title=""', ' title="new"', ('/region/p[0]/@title', '/region/p[0]/@title')),
        ]:
            with self.subTest(old=old, new=new):
                report = compare_html(f'<main><p{old}>Keep</p></main>', f'<main><p{new}>Keep</p></main>', rules)
                finding = report['suppressed'][0]
                self.assertIn('before_path', finding)
                self.assertIn('after_path', finding)
                self.assertEqual((finding['before_path'], finding['after_path']), locations)
        identical = '<main><p>Keep</p></main>'
        self.assertEqual(compare_html(identical, identical, rules)['suppressed'], [])

    def test_unmatched_volatile_target_observation_has_null_location(self):
        # Selector membership changes, but the actual attribute remains semantic on one side.
        rules = {**BASE, 'volatile_attributes': [{'selector': '.render', 'attributes': ['title'], 'min_matches': 0}]}
        before = '<main><p title="old">Keep</p></main>'
        after = '<main><p class="render" title="new">Keep</p></main>'
        for a, b, locations in [(before, after, (None, '/region/p[0]/@title')),
                                (after, before, ('/region/p[0]/@title', None))]:
            report = compare_html(a, b, rules)
            finding = report['suppressed'][0]
            self.assertIn('before_path', finding)
            self.assertIn('after_path', finding)
            self.assertEqual((finding['before_path'], finding['after_path']), locations)
            self.assertTrue(report['changes'])

    def test_keyed_collection_and_item_locations_need_no_sibling_identity(self):
        rules = {**BASE, 'unordered_collections': [{'selector': 'section', 'items': 'article', 'key_attribute': 'id'}],
                 'volatile_attributes': [{'selector': 'article', 'attributes': ['title']}]}
        before = '<main><section><article id="a/b~c" title="old">A</article><article id="B">B</article></section></main>'
        after = '<main><section><article id="B">B</article><article id="a/b~c" title="new">A</article></section></main>'
        report = compare_html(before, after, rules)
        self.assertEqual(report['changes'], [])
        self.assertEqual(len(report['suppressed']), 2)
        for finding in report['suppressed']:
            expected = ('/region/section[0]', '/region/section[0]') if finding['rule'] == 'unordered_collection' else (
                '/region/section[0]/article[0]/@title', '/region/section[0]/article[1]/@title')
            self.assertEqual((finding.get('before_path'), finding.get('after_path')), expected)
        self.assertIn('a~1b~0c', next(f['path'] for f in report['suppressed'] if f['rule'] == 'volatile_attributes'))

    def test_ambiguous_identity_observations_stay_audited_without_suppression_paths(self):
        rules = {**BASE, 'sibling_identity': [{'selector': 'article', 'key_attribute': 'id'}],
                 'ignore_selectors': ['aside'], 'normalize_whitespace': True,
                 'volatile_attributes': [{'selector': 'article', 'attributes': ['title']}]}
        def card(key, value):
            return f'<article {key} title="{value}"><aside>{value}</aside><p> {value} </p></article>'
        for key in ('id="A"', ''):
            duplicate = '<main>' + card(key, 'one') + card(key, 'two') + '</main>'
            unique = '<main>' + card(key, 'three') + '</main>'
            for a, b in ((duplicate, unique), (unique, duplicate)):
                with self.subTest(key=key, a=a):
                    report = compare_html(a, b, rules)
                    self.assertEqual(report['suppressed'], [])
                    for side in ('before', 'after'):
                        self.assertTrue(report['normalizations'][side])
                        self.assertTrue(all('original_path' in e for e in report['normalizations'][side]))
                    self.assertIn('ambiguous', [c['kind'] for c in report['changes']])

    def test_ambiguity_blocks_one_sided_ignore_even_without_ambiguous_observations(self):
        rules = {**BASE, 'sibling_identity': [{'selector': 'article', 'key_attribute': 'id'}],
                 'ignore_selectors': [{'selector': 'aside', 'min_matches': 0}]}
        duplicate = '<main><article id="A">One</article><article id="A">Two</article></main>'
        unique = '<main><article id="A">One<aside>Noise</aside></article></main>'
        for a, b, empty_side in ((duplicate, unique, 'before'), (unique, duplicate, 'after')):
            with self.subTest(empty_side=empty_side):
                report = compare_html(a, b, rules)
                self.assertEqual(report['normalizations'][empty_side], [])
                self.assertEqual(report['suppressed'], [])
                self.assertEqual(report['changes'][0]['kind'], 'ambiguous')

    def test_one_sided_ignore_inside_reordered_identity_keeps_present_location(self):
        rules = {**BASE, 'sibling_identity': [{'selector': 'article', 'key_attribute': 'id'}],
                 'ignore_selectors': [{'selector': 'aside', 'min_matches': 0}]}
        before = '<main><article id="A">A<aside>Noise</aside></article><article id="B">B</article></main>'
        after = '<main><article id="B">B</article><article id="A">A</article></main>'
        for a, b, locations in ((before, after, ('/region/article[0]/aside[0]', None)),
                                (after, before, (None, '/region/article[0]/aside[0]'))):
            report = compare_html(a, b, rules)
            self.assertEqual([c['kind'] for c in report['changes']], ['moved', 'moved'])
            self.assertEqual(len(report['suppressed']), 1)
            finding = report['suppressed'][0]
            self.assertEqual((finding['before_path'], finding['after_path']), locations)

    def test_markdown_suppression_section_exposes_locations_and_null_without_audit(self):
        report = compare_files(EXAMPLES / 'before.html', EXAMPLES / 'after.html', load_rules(EXAMPLES / 'rules.json'))
        original = deepcopy(report)
        section = markdown(report).split('## Suppressed differences\n', 1)[1].split('## Normalization rule coverage', 1)[0]
        self.assertEqual(section.count('before location:'), 5)
        self.assertEqual(section.count('after location:'), 5)
        self.assertIn('/region/article[2]/aside[0]', section)
        self.assertEqual(report, original)
        absent = compare_html('<main>Keep</main>', '<main>Keep<aside>Noise</aside></main>',
                              {**BASE, 'ignore_selectors': [{'selector': 'aside', 'min_matches': 0}]})
        self.assertIn('before location: `null`', markdown(absent))

    def test_cli_json_markdown_and_invalid_input_preserves_output(self):
        command = [sys.executable, '-m', 'qawrench.page_diff', str(EXAMPLES / 'before.html'),
                   str(EXAMPLES / 'after.html'), '--rules', str(EXAMPLES / 'rules.json'), '--fail-on-change']
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(len(report['suppressed']), 5)
        self.assertTrue(all('before_path' in f and 'after_path' in f for f in report['suppressed']))
        result = subprocess.run(command + ['--format', 'markdown'], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('before location:', result.stdout.split('## Suppressed differences', 1)[1])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'report.json'
            output.write_text('previous report')
            invalid = Path(directory) / 'invalid.html'
            invalid.write_text('<main></main>')
            result = subprocess.run(command[:4] + [str(invalid)] + command[5:] + ['--out', str(output)],
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertEqual(output.read_text(), 'previous report')
