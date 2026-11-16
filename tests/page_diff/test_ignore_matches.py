"""Individually located matches within one outermost ignored subtree."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from qawrench.page_diff import ContractError, compare_files, compare_html, load_rules
from qawrench.page_diff.cli import markdown

EXAMPLES = Path(__file__).resolve().parents[2] / 'examples' / 'page_diff' / 'suppression-rules'
BASE = {'version': 1, 'required_region': 'main'}


def ref(index, selector):
    return {'rule_id': f'ignore_selectors[{index}]', 'selector': selector}


def match(path, *rules):
    return {'original_path': path, 'rules': list(rules)}


class IgnoreMatchTests(unittest.TestCase):
    def test_reordered_and_one_sided_matches_keep_original_locations(self):
        report = compare_files(EXAMPLES / 'before.html', EXAMPLES / 'after.html', load_rules(EXAMPLES / 'rules.json'))
        roots = (ref(0, '.noise'), ref(1, '.noise'))
        for side, article, extra in (('before', 0, ref(2, '.old-noise')), ('after', 1, ref(3, '.new-noise'))):
            event = next(e for e in report['normalizations'][side] if e['rule'] == 'ignore_selectors' and '="A"' in e['path'])
            path = f'/region/article[{article}]/aside[0]'
            self.assertEqual(event['matched_nodes'], [match(path, *roots, extra), match(path + '/em[0]', ref(4, '.inner'))])
            self.assertEqual(event['rules'], list(roots) + [extra])
            self.assertIsNone(event['new'])
            self.assertIn('<em class="inner">', event['old'])
            # Coverage counts and individual match provenance agree per rule.
            events = [e for e in report['normalizations'][side] if e['rule'] == 'ignore_selectors']
            for row in report['rule_coverage'][side]:
                if row['rule_id'].startswith('ignore_selectors['):
                    count = sum(r['rule_id'] == row['rule_id'] for e in events for n in e['matched_nodes'] for r in n['rules'])
                    self.assertEqual(count, row['matched_elements'])
        b = next(e for e in report['normalizations']['after'] if e['rule'] == 'ignore_selectors' and '="B"' in e['path'])
        self.assertEqual(b['matched_nodes'], [match('/region/article[0]/aside[0]', *roots)])
        self.assertFalse(any(e['rule'] == 'ignore_selectors' and '="B"' in e['path'] for e in report['normalizations']['before']))
        self.assertEqual(len(report['suppressed']), 4)
        one_sided = next(f for f in report['suppressed'] if f['rule'] == 'ignore_selectors' and f['old'] is None)
        self.assertEqual((one_sided['before_rules'], one_sided['before_path']), ([], None))
        self.assertEqual(one_sided['after_rules'], list(roots))
        self.assertTrue(all('matched_nodes' not in e for e in report['normalizations']['before'] if e['rule'] != 'ignore_selectors'))
        self.assertEqual(sorted(c['kind'] for c in report['changes']), ['changed', 'moved', 'moved'])
        edit = next(c for c in report['changes'] if c['kind'] == 'changed')
        self.assertEqual((edit['old'], edit['new'], edit['before_path'], edit['after_path']),
                         ('$10', '$12', '/region/article[0]/p[0]/#text[0]', '/region/article[1]/p[0]/#text[0]'))

    def test_dom_order_deduplication_and_all_nested_rule_ids(self):
        # Descendant rules precede the root rule, and a selector matches both levels.
        selectors = ['em', 'aside, em', '.leaf', '.leaf', 'strong']
        rules = {**BASE, 'ignore_selectors': selectors}
        page = '<main>Keep<aside><i>unmatched</i><em class="leaf"><strong>old</strong></em><em class="leaf">two</em></aside><aside><em class="leaf">three</em></aside></main>'
        report = compare_html(page, page.replace('old', 'new'), rules)
        events = report['normalizations']['before']
        root = '/region/aside[0]'
        leaf_rules = (ref(0, 'em'), ref(1, 'aside, em'), ref(2, '.leaf'), ref(3, '.leaf'))
        self.assertEqual(events[0]['matched_nodes'], [
            match(root, ref(1, 'aside, em')), match(root + '/em[0]', *leaf_rules),
            match(root + '/em[0]/strong[0]', ref(4, 'strong')), match(root + '/em[1]', *leaf_rules)])
        self.assertEqual(events[1]['matched_nodes'], [match('/region/aside[1]', ref(1, 'aside, em')),
                                                     match('/region/aside[1]/em[0]', *leaf_rules)])
        self.assertEqual(len(events), 2)
        self.assertEqual(len(report['suppressed']), 1)
        self.assertEqual(report['suppressed'][0]['before_rules'], [ref(1, 'aside, em')])
        self.assertEqual(report['changes'], [])
        self.assertEqual(compare_html(page, page, rules)['suppressed'], [])

    def test_ambiguous_candidates_are_located_without_suppression(self):
        rules = {**BASE, 'sibling_identity': [{'selector': 'article', 'key_attribute': 'id'}],
                 'ignore_selectors': [{'selector': 'aside, em', 'min_matches': 0}]}
        for key in (' id="A"', ''):
            duplicate = f'<main><article{key}>One<aside><em>first</em></aside></article><article{key}>Two<aside><em>second</em></aside></article></main>'
            unique = f'<main><article{key}>One</article></main>'
            for a, b, side in ((duplicate, unique, 'before'), (unique, duplicate, 'after')):
                with self.subTest(key=key, side=side):
                    report = compare_html(a, b, rules)
                    self.assertEqual(report['suppressed'], [])
                    self.assertEqual(report['changes'][0]['kind'], 'ambiguous')
                    events = report['normalizations'][side]
                    self.assertEqual(len(events), 2)
                    for i, event in enumerate(events):
                        path = f'/region/article[{i}]/aside[0]'
                        self.assertEqual(event['matched_nodes'], [match(path, ref(0, 'aside, em')),
                                                                 match(path + '/em[0]', ref(0, 'aside, em'))])
                        self.assertTrue(event['ambiguous_identity'])

    def test_coverage_and_protected_nodes_still_fail(self):
        page = '<main>Keep<aside><em>Noise</em><em>More</em></aside></main>'
        for entry in ({'selector': 'em', 'max_matches': 1}, {'selector': '.absent'},
                      {'selector': 'em', 'expires': '2000-01-01'}):
            with self.subTest(entry=entry), self.assertRaises(ContractError):
                compare_html(page, page, {**BASE, 'ignore_selectors': ['aside', entry]})
        with self.assertRaisesRegex(ContractError, 'ignore selector removes'):
            compare_html(page, page, {**BASE, 'ignore_selectors': ['em', 'main']})
        with self.assertRaisesRegex(ContractError, 'ignore selector removes'):
            compare_html(page, page, {**BASE, 'sibling_identity': [{'selector': 'em', 'key_attribute': 'id'}],
                                     'ignore_selectors': ['aside']})

    def test_markdown_audit_retains_matches_and_escapes_selectors(self):
        rules = {**BASE, 'ignore_selectors': ['aside', '[data-note="<`>"]']}
        page = '<main>Keep<aside><em data-note="&lt;`&gt;">old</em></aside></main>'
        report = compare_html(page, page.replace('old', 'new'), rules)
        original = deepcopy(report)
        audit = markdown(report).split('## Full normalization audit\n', 1)[1].split('```json\n', 1)[1].split('\n```', 1)[0]
        self.assertEqual(json.loads(audit), report['normalizations'])
        self.assertIn('"matched_nodes":', audit)
        self.assertIn('/region/aside[0]/em[0]', audit)
        self.assertIn('\\u003c\\u0060\\u003e', audit)
        self.assertNotIn('<`>', audit)
        self.assertEqual(report, original)

    def test_cli_json_markdown_and_nested_bound_failure(self):
        command = [sys.executable, '-m', 'qawrench.page_diff', str(EXAMPLES / 'before.html'),
                   str(EXAMPLES / 'after.html'), '--rules', str(EXAMPLES / 'rules.json'), '--fail-on-change']
        for fmt in ('json', 'markdown'):
            result = subprocess.run(command + ['--format', fmt], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 1, result.stderr)
            if fmt == 'json':
                event = json.loads(result.stdout)['normalizations']['before'][0]
                self.assertEqual(event['matched_nodes'][-1], match('/region/article[0]/aside[0]/em[0]', ref(4, '.inner')))
            else:
                self.assertIn('"matched_nodes":', result.stdout)
                self.assertIn('/region/article[1]/aside[0]/em[0]', result.stdout)
        with tempfile.TemporaryDirectory() as directory:
            rules_path = Path(directory) / 'rules.json'
            rules = load_rules(EXAMPLES / 'rules.json')
            rules['ignore_selectors'][4] = {'selector': '.inner', 'min_matches': 0, 'max_matches': 0}
            rules_path.write_text(json.dumps(rules))
            output = Path(directory) / 'report.json'
            for existing in (False, True):
                if existing:
                    output.write_text('previous report')
                bad = command[:6] + [str(rules_path), '--out', str(output)]
                result = subprocess.run(bad, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('ignore_selectors[4]', result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertEqual(output.read_text() if existing else output.exists(), 'previous report' if existing else False)
