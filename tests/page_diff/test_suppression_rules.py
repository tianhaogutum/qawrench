"""Per-observation rule provenance on synthetic offline normalization findings."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from qawrench.page_diff import compare_files, compare_html, load_rules
from qawrench.page_diff.cli import markdown

EXAMPLES = Path(__file__).resolve().parents[2] / 'examples' / 'page_diff' / 'suppression-rules'
BASE = {'version': 1, 'required_region': 'main'}


def ref(field, index, selector):
    return {'rule_id': f'{field}[{index}]', 'selector': selector}


class SuppressionRuleTests(unittest.TestCase):
    def test_reordered_overlapping_rules_retain_independent_provenance(self):
        report = compare_files(EXAMPLES / 'before.html', EXAMPLES / 'after.html', load_rules(EXAMPLES / 'rules.json'))
        ignores = [ref('ignore_selectors', i, '.noise') for i in (0, 1)]
        volatile = [ref('volatile_attributes', i, 'article') for i in (0, 1)]
        self.assertEqual(len(report['suppressed']), 4)
        for finding in report['suppressed']:
            is_a = '="A"' in finding['path']
            if finding['rule'] == 'ignore_selectors':
                expected = (ignores + [ref('ignore_selectors', 2, '.old-noise')],
                            ignores + [ref('ignore_selectors', 3, '.new-noise')]) if is_a else ([], ignores)
                locations = ('/region/article[0]/aside[0]', '/region/article[1]/aside[0]') if is_a else (None, '/region/article[0]/aside[0]')
            else:
                expected = (volatile + [ref('volatile_attributes', 2, '[data-render^=old]')],
                            volatile + [ref('volatile_attributes', 3, '[data-render^=new]')])
                locations = ('/region/article[0]/@data-render', '/region/article[1]/@data-render') if is_a else (
                    '/region/article[1]/@data-render', '/region/article[0]/@data-render')
            self.assertEqual((finding['before_rules'], finding['after_rules']), expected)
            self.assertEqual((finding['before_path'], finding['after_path']), locations)
            for side, descriptors in zip(('before', 'after'), expected):
                events = [e for e in report['normalizations'][side] if (e['rule'], e['path']) == (finding['rule'], finding['path'])]
                self.assertEqual([e['rules'] for e in events], [descriptors] if descriptors else [])
        self.assertEqual(sorted(c['kind'] for c in report['changes']), ['changed', 'moved', 'moved'])
        edit = next(c for c in report['changes'] if c['kind'] == 'changed')
        self.assertEqual((edit['old'], edit['new'], edit['before_path'], edit['after_path']),
                         ('$10', '$12', '/region/article[0]/p[0]/#text[0]', '/region/article[1]/p[0]/#text[0]'))
        # Descendant-only ignore rules remain in coverage, not attributed to the outer root.
        for side in ('before', 'after'):
            self.assertEqual(next(r['matched_elements'] for r in report['rule_coverage'][side]
                                  if r['rule_id'] == 'ignore_selectors[4]'), 1)

    def test_missing_attribute_has_rules_but_unobserved_target_has_none(self):
        rules = {**BASE, 'volatile_attributes': [{'selector': '.render', 'attributes': ['title'], 'min_matches': 0}]}
        descriptors = [ref('volatile_attributes', 0, '.render')]
        for attrs, expected in [('', []), (' class="render"', descriptors), (' class="render" title=""', descriptors)]:
            before = f'<main><p{attrs}>Keep</p></main>'
            after = '<main><p class="render" title="new">Keep</p></main>'
            for a, b, pairs in ((before, after, (expected, descriptors)), (after, before, (descriptors, expected))):
                with self.subTest(attrs=attrs, a=a):
                    finding = compare_html(a, b, rules)['suppressed'][0]
                    self.assertEqual((finding['before_rules'], finding['after_rules']), pairs)
                    self.assertEqual((finding['before_path'], finding['after_path']),
                                     ('/region/p[0]/@title' if 'title=' in a else None,
                                      '/region/p[0]/@title' if 'title=' in b else None))
        # An unselected existing attribute still has semantic significance.
        report = compare_html('<main><p title="old">Keep</p></main>', after, rules)
        self.assertEqual(report['suppressed'][0]['before_rules'], [])
        self.assertTrue(report['changes'])

    def test_ambiguous_candidates_keep_rules_only_in_audit(self):
        rules = {**BASE, 'sibling_identity': [{'selector': 'article', 'key_attribute': 'id'}],
                 'ignore_selectors': [{'selector': 'aside', 'min_matches': 0}],
                 'volatile_attributes': [{'selector': '[title]', 'attributes': ['title'], 'min_matches': 0}]}
        for key in (' id="A"', ''):
            duplicate = f'<main><article{key}>One</article><article{key}>Two</article></main>'
            unique = f'<main><article{key} title="new">One<aside>Noise</aside></article></main>'
            for a, b, side in ((duplicate, unique, 'after'), (unique, duplicate, 'before')):
                with self.subTest(key=key, side=side):
                    report = compare_html(a, b, rules)
                    self.assertEqual(report['suppressed'], [])
                    self.assertEqual(report['changes'][0]['kind'], 'ambiguous')
                    events = report['normalizations'][side]
                    self.assertEqual([e['rules'] for e in events], [[ref('ignore_selectors', 0, 'aside')],
                                                                               [ref('volatile_attributes', 0, '[title]')]])
                    self.assertTrue(all(e['original_path'] for e in events))

    def test_whitespace_and_collection_rules_have_consistent_descriptors(self):
        report = compare_html('<main><p> Keep </p></main>', '<main><p>Keep</p></main>',
                              {**BASE, 'normalize_whitespace': True})
        expected = [{'rule_id': 'normalize_whitespace', 'selector': None}]
        self.assertEqual(report['suppressed'][0]['before_rules'], expected)
        self.assertEqual(report['suppressed'][0]['after_rules'], expected)
        absent = compare_html('<main>Keep</main>', '<main>Keep<p> </p></main>', {**BASE, 'normalize_whitespace': True})
        # Removing whitespace in a surviving added element is still an auditable one-sided observation.
        self.assertEqual(absent['suppressed'][0]['before_rules'], [])
        self.assertEqual(absent['suppressed'][0]['after_rules'], expected)
        before = '<main><section><p id="A">A</p><p id="B">B</p></section></main>'
        after = '<main><section><p id="B">B</p><p id="A">A</p></section></main>'
        report = compare_html(before, after, {**BASE, 'unordered_collections': [
            {'selector': 'section', 'items': 'p', 'key_attribute': 'id'}]})
        expected = [ref('unordered_collections', 0, 'section')]
        self.assertEqual(report['suppressed'][0]['before_rules'], expected)
        self.assertEqual(report['suppressed'][0]['after_rules'], expected)

    def test_provenance_does_not_create_new_suppression_claims(self):
        rules = {**BASE, 'normalize_whitespace': True, 'ignore_selectors': ['aside']}
        page = '<main><aside>Noise</aside><p> Keep </p></main>'
        identical = compare_html(page, page, rules)
        self.assertEqual(identical['suppressed'], [])
        self.assertTrue(all(e['rules'] for e in identical['normalizations']['before']))
        substantive = compare_html(page, page.replace('Keep', 'Changed'), rules)
        self.assertEqual(substantive['suppressed'], [])
        self.assertEqual(substantive['changes'][0]['new'], 'Changed')

    def test_markdown_exposes_provenance_without_audit_and_escapes_selectors(self):
        rules = {**BASE, 'ignore_selectors': [{'selector': '[data-note="<`>"]', 'min_matches': 0}]}
        report = compare_html('<main>Keep</main>', '<main>Keep<aside data-note="&lt;`&gt;">Noise</aside></main>', rules)
        original = deepcopy(report)
        section = markdown(report).split('## Suppressed differences\n', 1)[1].split('## Normalization rule coverage', 1)[0]
        self.assertIn('before rules: `[]`', section)
        self.assertIn('after rules: `[{"rule_id": "ignore_selectors[0]", "selector":', section)
        self.assertIn('\\u003c\\u0060\\u003e', section)
        self.assertNotIn('<`>', section)
        self.assertEqual(report, original)

    def test_cli_json_markdown_and_no_report_error(self):
        command = [sys.executable, '-m', 'qawrench.page_diff', str(EXAMPLES / 'before.html'),
                   str(EXAMPLES / 'after.html'), '--rules', str(EXAMPLES / 'rules.json'), '--fail-on-change']
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(len(report['suppressed']), 4)
        self.assertTrue(all('before_rules' in f and 'after_rules' in f for f in report['suppressed']))
        result = subprocess.run(command + ['--format', 'markdown'], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1, result.stderr)
        section = result.stdout.split('## Suppressed differences', 1)[1].split('## Normalization rule coverage', 1)[0]
        self.assertEqual(section.count('before rules:'), 4)
        self.assertEqual(section.count('after rules:'), 4)
        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory) / 'invalid.html'
            invalid.write_text('<main></main>')
            output = Path(directory) / 'report.json'
            bad = command[:4] + [str(invalid)] + command[5:] + ['--out', str(output)]
            for existing in (False, True):
                if existing:
                    output.write_text('previous report')
                result = subprocess.run(bad, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertEqual(output.read_text() if existing else output.exists(), 'previous report' if existing else False)
