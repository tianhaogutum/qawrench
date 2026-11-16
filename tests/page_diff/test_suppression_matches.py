"""Per-side nested ignore evidence directly on suppressed findings."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import unittest

from qawrench.page_diff import compare_files, compare_html, load_rules
from qawrench.page_diff.cli import markdown

EXAMPLES = Path(__file__).resolve().parents[2] / 'examples' / 'page_diff' / 'suppression-rules'
BASE = {'version': 1, 'required_region': 'main'}


def match(path, *rules):
    return {'original_path': path, 'rules': [
        {'rule_id': f'ignore_selectors[{i}]', 'selector': selector} for i, selector in rules]}


def suppression_section(text):
    return text.split('## Suppressed differences\n', 1)[1].split('## Normalization rule coverage', 1)[0]


class SuppressionMatchTests(unittest.TestCase):
    def test_reordered_matches_project_audit_and_preserve_findings(self):
        report = compare_files(EXAMPLES / 'before.html', EXAMPLES / 'after.html', load_rules(EXAMPLES / 'rules.json'))
        self.assertEqual(len(report['suppressed']), 4)
        a = next(f for f in report['suppressed'] if f['rule'] == 'ignore_selectors' and '="A"' in f['path'])
        for side, article, extra in (('before', 0, (2, '.old-noise')), ('after', 1, (3, '.new-noise'))):
            root = f'/region/article[{article}]/aside[0]'
            expected = [match(root, (0, '.noise'), (1, '.noise'), extra), match(root + '/em[0]', (4, '.inner'))]
            self.assertEqual(a[side + '_matched_nodes'], expected)
            self.assertEqual(a[side + '_rules'], expected[0]['rules'])
            self.assertEqual(a[side + '_path'], root)
        for finding in report['suppressed']:
            for side in ('before', 'after'):
                field = side + '_matched_nodes'
                if finding['rule'] == 'ignore_selectors':
                    event = next((e for e in report['normalizations'][side]
                                  if (e['rule'], e['path']) == (finding['rule'], finding['path'])), None)
                    self.assertEqual(finding[field], event['matched_nodes'] if event else [])
                    self.assertEqual(finding['old' if side == 'before' else 'new'], event['old'] if event else None)
                else:
                    self.assertNotIn(field, finding)
        self.assertIsNone(a['normalized'])
        self.assertEqual(sorted(c['kind'] for c in report['changes']), ['changed', 'moved', 'moved'])
        edit = next(c for c in report['changes'] if c['kind'] == 'changed')
        self.assertEqual((edit['old'], edit['new'], edit['before_path'], edit['after_path']),
                         ('$10', '$12', '/region/article[0]/p[0]/#text[0]', '/region/article[1]/p[0]/#text[0]'))

    def test_independent_shapes_dom_order_and_repeated_nested_rules(self):
        # Node arrays are independently ordered evidence, never descendant pairs.
        rules = {**BASE, 'ignore_selectors': ['em', 'aside, em', 'em', {'selector': 'strong', 'min_matches': 0}]}
        before = '<main>Keep<aside><i>unmatched</i><em>one<strong>deep</strong></em><em>two</em></aside></main>'
        after = '<main>Keep<aside><em>new</em></aside></main>'
        finding = compare_html(before, after, rules)['suppressed'][0]
        root = '/region/aside[0]'
        leaf = ((0, 'em'), (1, 'aside, em'), (2, 'em'))
        self.assertEqual(finding['before_matched_nodes'], [match(root, (1, 'aside, em')),
                         match(root + '/em[0]', *leaf), match(root + '/em[0]/strong[0]', (3, 'strong')),
                         match(root + '/em[1]', *leaf)])
        self.assertEqual(finding['after_matched_nodes'], [match(root, (1, 'aside, em')), match(root + '/em[0]', *leaf)])
        self.assertEqual(finding['before_rules'], finding['after_rules'])
        self.assertEqual(finding['before_rules'], match(root, (1, 'aside, em'))['rules'])
        self.assertEqual(compare_html(before, before, rules)['suppressed'], [])
        # Other normalization types retain their existing shape.
        whitespace = compare_html('<main> Keep </main>', '<main>Keep</main>', {**BASE, 'normalize_whitespace': True})
        self.assertNotIn('before_matched_nodes', whitespace['suppressed'][0])
        self.assertNotIn('after_matched_nodes', whitespace['suppressed'][0])

    def test_absent_side_uses_empty_array_in_both_directions(self):
        rules = {**BASE, 'sibling_identity': [{'selector': 'article', 'key_attribute': 'id'}],
                 'ignore_selectors': [{'selector': 'aside, em', 'min_matches': 0}]}
        before = '<main><article id="A">A<aside><em>Noise</em></aside></article><article id="B">B</article></main>'
        after = '<main><article id="B">B</article><article id="A">A</article></main>'
        for a, b, present_side, absent_side in ((before, after, 'before', 'after'), (after, before, 'after', 'before')):
            with self.subTest(present=present_side):
                report = compare_html(a, b, rules)
                self.assertEqual(len(report['suppressed']), 1)
                finding = report['suppressed'][0]
                root = '/region/article[0]/aside[0]'
                self.assertEqual(finding[present_side + '_matched_nodes'], [match(root, (0, 'aside, em')),
                                 match(root + '/em[0]', (0, 'aside, em'))])
                self.assertEqual(finding[absent_side + '_matched_nodes'], [])
                self.assertEqual(finding[absent_side + '_rules'], [])
                self.assertIsNone(finding[absent_side + '_path'])
                self.assertEqual([c['kind'] for c in report['changes']], ['moved', 'moved'])

    def test_ambiguity_keeps_audit_without_claims_and_allows_unambiguous_peer(self):
        rules = {**BASE, 'sibling_identity': [{'selector': 'article', 'key_attribute': 'id'}],
                 'ignore_selectors': [{'selector': 'aside, em', 'min_matches': 0}]}
        for key in (' id="A"', ''):
            duplicate = f'<main><article{key}>One</article><article{key}>Two</article><article id="B">B<aside><em>old</em></aside></article></main>'
            unique = f'<main><article{key}>One<aside><em>Noise</em></aside></article><article id="B">B<aside><em>new</em></aside></article></main>'
            for a, b in ((duplicate, unique), (unique, duplicate)):
                with self.subTest(key=key, a=a):
                    report = compare_html(a, b, rules)
                    self.assertEqual(len(report['suppressed']), 1)
                    finding = report['suppressed'][0]
                    self.assertIn('="B"', finding['path'])
                    for side, page in (('before', a), ('after', b)):
                        index = 2 if page == duplicate else 1
                        root = f'/region/article[{index}]/aside[0]'
                        self.assertEqual(finding[side + '_matched_nodes'], [match(root, (0, 'aside, em')),
                                         match(root + '/em[0]', (0, 'aside, em'))])
                    # Ambiguous counterpart has no observations on the duplicate side.
                    side = 'after' if b == unique else 'before'
                    event = next(e for e in report['normalizations'][side] if '/article[0]/' in e['original_path'])
                    self.assertEqual(event['matched_nodes'][-1], match('/region/article[0]/aside[0]/em[0]', (0, 'aside, em')))
                    self.assertIn('ambiguous', [c['kind'] for c in report['changes']])

    def test_markdown_exposes_independent_arrays_without_full_audit(self):
        rules = {**BASE, 'ignore_selectors': [{'selector': 'aside', 'min_matches': 0},
                                             {'selector': '[data-note="<`>"]', 'min_matches': 0}]}
        report = compare_html('<main>Keep</main>', '<main>Keep<aside><em data-note="&lt;`&gt;">Noise</em></aside></main>', rules)
        original = deepcopy(report)
        section = suppression_section(markdown(report))
        finding = report['suppressed'][0]
        for side in ('before', 'after'):
            line = next(line for line in section.splitlines() if f'{side} matched nodes:' in line)
            self.assertEqual(json.loads(line.split('`', 1)[1].rsplit('`', 1)[0]), finding[side + '_matched_nodes'])
        self.assertIn('before matched nodes: `[]`', section)
        self.assertIn('/region/aside[0]/em[0]', section)
        self.assertIn('\\u003c\\u0060\\u003e', section)
        self.assertNotIn('<`>', section)
        self.assertEqual(report, original)

    def test_cli_json_and_markdown_include_match_evidence(self):
        command = [sys.executable, '-m', 'qawrench.page_diff', str(EXAMPLES / 'before.html'),
                   str(EXAMPLES / 'after.html'), '--rules', str(EXAMPLES / 'rules.json'), '--fail-on-change']
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        finding = next(f for f in report['suppressed'] if f['rule'] == 'ignore_selectors' and '="A"' in f['path'])
        self.assertEqual(finding['after_matched_nodes'][-1], match('/region/article[1]/aside[0]/em[0]', (4, '.inner')))
        result = subprocess.run(command + ['--format', 'markdown'], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1, result.stderr)
        section = suppression_section(result.stdout)
        self.assertEqual(section.count('before matched nodes:'), 2)
        self.assertEqual(section.count('after matched nodes:'), 2)
        self.assertIn('/region/article[0]/aside[0]/em[0]', section)
        self.assertIn('/region/article[1]/aside[0]/em[0]', section)
