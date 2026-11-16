"""Matched element locations distinguish missing attributes from absent observations."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from qawrench.page_diff import compare_files, compare_html, load_rules
from qawrench.page_diff.cli import markdown

EXAMPLES = Path(__file__).resolve().parents[2] / 'examples' / 'page_diff' / 'suppression-targets'
BASE = {'version': 1, 'required_region': 'main'}


def suppression_section(text):
    return text.split('## Suppressed differences\n', 1)[1].split('## Normalization rule coverage', 1)[0]


class SuppressionTargetTests(unittest.TestCase):
    def test_reordered_missing_and_empty_attributes_retain_original_element_targets(self):
        report = compare_files(EXAMPLES / 'before.html', EXAMPLES / 'after.html', load_rules(EXAMPLES / 'rules.json'))
        self.assertEqual(len(report['suppressed']), 3)
        attrs = {f['new']: f for f in report['suppressed'] if f['rule'] == 'volatile_attributes'}
        for value, old, before, after in [('new-a', None, 1, 2), ('new-b', '', 2, 1)]:
            with self.subTest(value=value):
                finding = attrs[value]
                a, b = f'/region/article[{before}]', f'/region/article[{after}]'
                self.assertEqual((finding['before_target_path'], finding['after_target_path']), (a, b))
                self.assertEqual((finding['before_path'], finding['after_path']),
                                 (a + '/@data-render' if old is not None else None, b + '/@data-render'))
                self.assertEqual((finding['old'], finding['new'], finding['normalized']), (old, value, None))
                self.assertEqual([r['rule_id'] for r in finding['before_rules']],
                                 ['volatile_attributes[0]'] if old is None else ['volatile_attributes[0]', 'volatile_attributes[1]'])
                self.assertEqual([r['rule_id'] for r in finding['after_rules']], ['volatile_attributes[0]', 'volatile_attributes[1]'])
        ignore = next(f for f in report['suppressed'] if f['rule'] == 'ignore_selectors')
        self.assertEqual(ignore['before_rules'], [{'rule_id': 'ignore_selectors[1]', 'selector': '.noise'}])
        self.assertEqual(ignore['before_matched_nodes'][-1], {
            'original_path': '/region/article[1]/aside[0]/em[0]',
            'rules': [{'rule_id': f'ignore_selectors[{i}]', 'selector': '.inner'} for i in (2, 3)]})
        self.assertEqual(ignore['after_matched_nodes'][-1]['original_path'], '/region/article[2]/aside[0]/em[0]')
        self.assertNotIn('before_target_path', ignore)
        self.assertNotIn('after_target_path', ignore)
        self.assertEqual(sorted(c['kind'] for c in report['changes']), ['changed', 'moved', 'moved'])
        edit = next(c for c in report['changes'] if c['kind'] == 'changed')
        self.assertEqual((edit['old'], edit['new'], edit['before_path'], edit['after_path']),
                         ('$10', '$12', '/region/article[1]/p[0]/#text[0]', '/region/article[2]/p[0]/#text[0]'))
        self.assertTrue(all('before_target_path' not in c and 'after_target_path' not in c for c in report['changes']))

    def test_unobserved_counterpart_uses_null_even_when_element_and_attribute_exist(self):
        rules = {**BASE, 'volatile_attributes': [{'selector': '.render', 'attributes': ['title'], 'min_matches': 0}]}
        before = '<main><p title="semantic">Keep</p></main>'
        after = '<main><p class="render" title="noise">Keep</p></main>'
        for a, b, absent in ((before, after, 'before'), (after, before, 'after')):
            with self.subTest(absent=absent):
                report = compare_html(a, b, rules)
                self.assertEqual(len(report['suppressed']), 1)
                finding = report['suppressed'][0]
                present = 'after' if absent == 'before' else 'before'
                self.assertIsNone(finding[absent + '_target_path'])
                self.assertIsNone(finding[absent + '_path'])
                self.assertEqual(finding[absent + '_rules'], [])
                self.assertEqual(finding[present + '_target_path'], '/region/p[0]')
                self.assertEqual(finding[present + '_path'], '/region/p[0]/@title')
                edit = next(c for c in report['changes'] if c['path'].endswith('/attributes/title'))
                self.assertEqual((edit['old'], edit['new']), ('semantic', None) if absent == 'before' else (None, 'semantic'))

    def test_region_root_and_keyed_collection_targets_with_multiple_attributes(self):
        rules = {**BASE, 'unordered_collections': [{'selector': 'section', 'items': 'article', 'key_attribute': 'id'}],
                 'volatile_attributes': [{'selector': 'main, article', 'attributes': ['title', 'data-render']}]}
        before = '<main title=""><section><article id="a/b~c">A</article><article id="B">B</article></section></main>'
        after = '<main title="new"><section><article id="B">B</article><article id="a/b~c" title="new" data-render="x">A</article></section></main>'
        report = compare_html(before, after, rules)
        self.assertEqual(report['changes'], [])
        self.assertEqual(len(report['suppressed']), 4)
        for f in report['suppressed']:
            if f['rule'] == 'volatile_attributes':
                expected = ('/region/section[0]/article[0]', '/region/section[0]/article[1]') if 'item[' in f['path'] else ('/region', '/region')
                self.assertEqual((f['before_target_path'], f['after_target_path']), expected)
            else:
                self.assertNotIn('before_target_path', f)
                self.assertNotIn('after_target_path', f)
        self.assertIn('a~1b~0c', next(f['path'] for f in report['suppressed'] if 'item[' in f['path']))
        same = '<main>Keep</main>'
        missing = compare_html(same, same, {**BASE, 'volatile_attributes': [{'selector': 'main', 'attributes': ['title']}]})
        self.assertEqual(missing['suppressed'], [])
        self.assertIsNone(missing['normalizations']['before'][0]['old'])
        whitespace = compare_html('<main> Keep </main>', same, {**BASE, 'normalize_whitespace': True})
        self.assertNotIn('before_target_path', whitespace['suppressed'][0])
        self.assertNotIn('after_target_path', whitespace['suppressed'][0])

    def test_ambiguous_identities_keep_audit_without_target_claims(self):
        rules = {**BASE, 'sibling_identity': [{'selector': 'article', 'key_attribute': 'id'}],
                 'volatile_attributes': [{'selector': '.render', 'attributes': ['title'], 'min_matches': 0}]}
        for key in (' id="A"', ''):
            duplicate = f'<main><article{key}>One</article><article{key}>Two</article></main>'
            unique = f'<main><article{key} class="render" title="noise">One</article></main>'
            for a, b in ((duplicate, unique), (unique, duplicate)):
                with self.subTest(key=key, a=a):
                    report = compare_html(a, b, rules)
                    self.assertEqual(report['suppressed'], [])
                    empty = 'before' if a == duplicate else 'after'
                    present = 'after' if empty == 'before' else 'before'
                    self.assertEqual(report['normalizations'][empty], [])
                    self.assertEqual(report['normalizations'][present][0]['original_path'], '/region/article[0]/@title')
                    self.assertEqual(report['changes'][0]['kind'], 'ambiguous')
        # A unique peer remains independently eligible beside ambiguous candidates.
        a = '<main><article id="A">One</article><article id="A">Two</article><article id="B" class="render">B</article></main>'
        b = '<main><article id="A">One</article><article id="B" class="render" title="new">B</article></main>'
        finding = compare_html(a, b, rules)['suppressed'][0]
        self.assertEqual((finding['before_target_path'], finding['after_target_path']), ('/region/article[2]', '/region/article[1]'))

    def test_markdown_exposes_target_locations_separately_from_attribute_locations(self):
        report = compare_files(EXAMPLES / 'before.html', EXAMPLES / 'after.html', load_rules(EXAMPLES / 'rules.json'))
        original = deepcopy(report)
        section = suppression_section(markdown(report))
        self.assertEqual(section.count('before target location:'), 2)
        self.assertEqual(section.count('after target location:'), 2)
        self.assertIn('before location: `null`', section)
        self.assertIn('before target location: `"/region/article[1]"`', section)
        self.assertEqual(report, original)
        one_sided = compare_html('<main><p>Keep</p></main>', '<main><p class="render" title="&lt;`&gt;">Keep</p></main>',
                                 {**BASE, 'volatile_attributes': [{'selector': '.render', 'attributes': ['title'], 'min_matches': 0}]})
        section = suppression_section(markdown(one_sided))
        self.assertIn('before target location: `null`', section)
        self.assertIn('after target location: `"/region/p[0]"`', section)
        self.assertIn('\\u003c\\u0060\\u003e', section)
        self.assertNotIn('<`>', section)

    def test_cli_examples_and_nested_bound_failures_preserve_outputs(self):
        command = [sys.executable, '-m', 'qawrench.page_diff', str(EXAMPLES / 'before.html'), str(EXAMPLES / 'after.html'),
                   '--rules', str(EXAMPLES / 'rules.json'), '--fail-on-change']
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1, result.stderr)
        a = next(f for f in json.loads(result.stdout)['suppressed'] if f['new'] == 'new-a')
        self.assertEqual((a['before_target_path'], a['after_target_path']), ('/region/article[1]', '/region/article[2]'))
        result = subprocess.run(command + ['--format', 'markdown'], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(suppression_section(result.stdout).count('before target location:'), 2)
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            rules = load_rules(EXAMPLES / 'rules.json')
            rules['ignore_selectors'] = [{'selector': '.noise, .inner', 'max_matches': 1}]
            path = td / 'rules.json'
            path.write_text(json.dumps(rules))
            output = td / 'report.json'
            bad = command[:command.index('--rules')] + ['--rules', str(path), '--out', str(output), '--fail-on-change']
            for existing, extra in ((False, []), (True, []), (True, ['--dry-run'])):
                if existing:
                    output.write_text('preserve this report')
                result = subprocess.run(bad + extra, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertIn('expected at most 1 matched elements, got 2', result.stderr)
                if existing:
                    self.assertEqual(output.read_text(), 'preserve this report')
                else:
                    self.assertFalse(output.exists())
