"""Direct per-rule original matches are independent of outermost-root audits."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from qawrench.page_diff import ContractError, compare_files, compare_html, load_rules
from qawrench.page_diff.cli import markdown

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'examples' / 'page_diff' / 'ignore-coverage'
BASE = {'version': 1, 'required_region': 'main'}


def nodes(*paths):
    return [{'original_path': p} for p in paths]


def ignore_rows(report, side):
    return [r for r in report['rule_coverage'][side] if r['rule_id'].startswith('ignore_selectors[')]


def markdown_coverage(text):
    return json.loads(text.split('## Normalization rule coverage\n', 1)[1].split('```json\n', 1)[1].split('\n```', 1)[0])


class IgnoreCoverageTests(unittest.TestCase):
    def inventories(self, report, side):
        rows = ignore_rows(report, side)
        for row in rows:
            self.assertEqual(len(row['matched_nodes']), row['matched_elements'])
            for node in row['matched_nodes']:
                self.assertEqual(set(node), {'original_path'})
            # Reconcile per-rule inventory against all directly attributed audit matches.
            audit_paths = [n['original_path'] for e in report['normalizations'][side]
                           if e['rule'] == 'ignore_selectors' for n in e['matched_nodes']
                           if any(r['rule_id'] == row['rule_id'] for r in n['rules'])]
            self.assertCountEqual(audit_paths, [n['original_path'] for n in row['matched_nodes']])
        return [r['matched_nodes'] for r in rows]

    def test_identical_nested_two_matches_have_direct_per_rule_inventory(self):
        rules = load_rules(FIXTURE / 'rules.json')
        original = deepcopy(rules)
        report = compare_files(FIXTURE / 'snapshot.html', FIXTURE / 'snapshot.html', rules)
        self.assertEqual((report['status'], report['changes'], report['suppressed']), ('unchanged', [], []))
        path = '/region/aside[0]'
        for side in ('before', 'after'):
            row = ignore_rows(report, side)[0]
            self.assertEqual(row['matched_elements'], 2)
            self.assertEqual(row['matched_nodes'], nodes(path, path + '/em[0]'))
            self.assertEqual(self.inventories(report, side), [nodes(path, path + '/em[0]')])
            audit = report['normalizations'][side]
            self.assertEqual(len(audit), 1)
            self.assertEqual(audit[0]['original_path'], path)
            self.assertEqual(audit[0]['rules'], [{'rule_id': 'ignore_selectors[0]', 'selector': '.noise'}])
            self.assertEqual([n['original_path'] for n in audit[0]['matched_nodes']], [path, path + '/em[0]'])
        self.assertEqual(markdown_coverage(markdown(report)), report['rule_coverage'])
        self.assertEqual(rules, original)

    def test_preorder_nested_overlapping_repeated_and_optional_rules(self):
        rules = {**BASE, 'ignore_selectors': ['em', 'aside, em, .leaf', '.leaf', '.leaf',
                                             {'selector': '.absent', 'min_matches': 0, 'max_matches': 0}]}
        # More than ten same-tag siblings distinguishes DOM from lexical path order.
        page = '<main>Keep<aside><i>Unselected</i>' + ''.join(
            f'<em class="leaf">{i}<strong class="leaf">Nested</strong></em>' for i in range(12)) + '</aside></main>'
        report = compare_html(page, page, rules)
        root = '/region/aside[0]'
        ems = [f'{root}/em[{i}]' for i in range(12)]
        leaves = [p for em in ems for p in (em, em + '/strong[0]')]
        for side in ('before', 'after'):
            self.assertEqual([r['rule_id'] for r in ignore_rows(report, side)], [f'ignore_selectors[{i}]' for i in range(5)])
            self.assertEqual(self.inventories(report, side), [nodes(*ems), nodes(root, *leaves), nodes(*leaves), nodes(*leaves), []])
            self.assertEqual(len(report['normalizations'][side]), 1)
        self.assertEqual(report['suppressed'], [])

    def test_reordered_identities_keep_side_membership_and_pre_removal_positions(self):
        rules = {**BASE, 'ignore_selectors': ['.noise', {'selector': '.old', 'min_matches': 0},
                                             {'selector': '.new', 'min_matches': 0}],
                 'sibling_identity': [{'selector': 'article', 'key_attribute': 'id'}]}
        a = '<article id="A"><p>Keep</p><p class="noise old"><em class="noise">Old</em></p><p>10</p></article>'
        b = '<article id="B"><p>20</p></article>'
        before = '<main>' + a + b + '</main>'
        after = '<main>' + b.replace('<p>', '<p class="noise new">New</p><p>') + a.replace('Old', 'New').replace(' old', ' new').replace('10', '11') + '</main>'
        report = compare_html(before, after, rules)
        old = '/region/article[0]/p[1]'
        new_b, new_a = '/region/article[0]/p[0]', '/region/article[1]/p[1]'
        self.assertEqual(self.inventories(report, 'before'), [nodes(old, old + '/em[0]'), nodes(old), []])
        self.assertEqual(self.inventories(report, 'after'), [nodes(new_b, new_a, new_a + '/em[0]'), [], nodes(new_b, new_a)])
        edit = next(c for c in report['changes'] if c['kind'] == 'changed')
        self.assertEqual((edit['old'], edit['new'], edit['before_path'], edit['after_path']),
                         ('10', '11', '/region/article[0]/p[2]/#text[0]', '/region/article[1]/p[2]/#text[0]'))
        self.assertEqual(sorted(c['kind'] for c in report['changes']), ['changed', 'moved', 'moved'])
        suppressions = [s for s in report['suppressed'] if s['rule'] == 'ignore_selectors']
        self.assertEqual(len(suppressions), 2)
        paired = next(s for s in suppressions if s['old'] is not None)
        self.assertEqual((paired['before_path'], paired['after_path']), (old, new_a))
        self.assertEqual(paired['before_rules'], [{'rule_id': 'ignore_selectors[0]', 'selector': '.noise'},
                                                {'rule_id': 'ignore_selectors[1]', 'selector': '.old'}])
        identity = next(r for r in report['rule_coverage']['after'] if r['rule_id'] == 'sibling_identity[0]')
        self.assertEqual([n['key'] for n in identity['matched_nodes']], ['B', 'A'])

    def test_ambiguous_and_unselected_counterparts_do_not_infer_membership(self):
        rules = {**BASE, 'ignore_selectors': [{'selector': '.noise', 'min_matches': 0}],
                 'sibling_identity': [{'selector': 'article', 'key_attribute': 'id'}]}
        before = '<main><article id="A">One<aside class="noise">N1</aside></article><article id="A">Two<aside class="noise">N2</aside></article></main>'
        after = before.replace(' class="noise"', '')
        for a, b in ((before, after), (after, before)):
            report = compare_html(a, b, rules)
            for side, html in (('before', a), ('after', b)):
                expected = nodes('/region/article[0]/aside[0]', '/region/article[1]/aside[0]') if 'class=' in html else []
                self.assertEqual(self.inventories(report, side), [expected])
            self.assertEqual(report['suppressed'], [])
            self.assertTrue(all(c['kind'] == 'ambiguous' for c in report['changes']))

    def test_collection_and_required_attribute_evidence_survives_ignores(self):
        rules = {**BASE, 'ignore_selectors': ['.noise'],
                 'unordered_collections': [{'selector': 'section', 'items': 'article', 'key_attribute': 'id'}],
                 'required_attributes': [{'selector': 'a', 'attributes': ['href']}]}
        a = '<article id="Z"><a class="noise">Ignore</a><a href="/one">Keep</a></article>'
        b = '<article id="A">Other<aside class="noise">Ignore</aside></article>'
        report = compare_html('<main><section>'+a+b+'</section></main>', '<main><section>'+b+a+'</section></main>',
                              {**rules, 'required_attributes': [{'selector': 'a[href]', 'attributes': ['href']}]})
        for side, article in (('before', 0), ('after', 1)):
            root = f'/region/section[0]/article[{article}]'
            inventory = self.inventories(report, side)[0]
            self.assertIn({'original_path': root + '/a[0]'}, inventory)
            required = next(r for r in report['rule_coverage'][side] if r['rule_id'] == 'required_attributes[0]')
            self.assertEqual(required['after_ignores']['matched_nodes'], nodes(root + '/a[1]'))
            collection = next(r for r in report['rule_coverage'][side] if r['rule_id'] == 'unordered_collections[0]')
            self.assertEqual([n['key'] for n in collection['matched_items']], ['Z', 'A'] if side == 'before' else ['A', 'Z'])
        self.assertEqual(report['status'], 'unchanged')
        page = '<main>Keep<a href="/one">Required</a><aside>Keep too</aside></main>'
        with self.assertRaisesRegex(ContractError, 'stage=after_ignores.*removed element'):
            compare_html(page, page, {**BASE, 'ignore_selectors': ['a'],
                                      'required_attributes': [{'selector': 'a', 'attributes': ['href']}]})
        with self.assertRaisesRegex(ContractError, 'ignore selector removes'):
            compare_html(page, page, {**BASE, 'ignore_selectors': ['main']})

    def test_markdown_coverage_escapes_selector_without_mutating_report(self):
        selector = '[data-note="<`é>"]'
        page = '<main>Keep<aside data-note="&lt;`é&gt;">Noise</aside></main>'
        report = compare_html(page, page, {**BASE, 'ignore_selectors': [selector]})
        original = deepcopy(report)
        output = markdown(report)
        self.assertEqual(markdown_coverage(output), report['rule_coverage'])
        self.assertEqual(self.inventories(report, 'before'), [nodes('/region/aside[0]')])
        self.assertIn('\\u003c\\u0060\\u00e9\\u003e', output)
        self.assertNotIn('<`é>', output)
        self.assertEqual(report, original)

    def test_cli_json_markdown_and_nested_minimum_maximum_failures(self):
        command = [sys.executable, '-m', 'qawrench.page_diff', str(FIXTURE / 'snapshot.html'),
                   str(FIXTURE / 'snapshot.html'), '--rules', str(FIXTURE / 'rules.json')]
        for fmt in ('json', 'markdown'):
            result = subprocess.run(command + ['--format', fmt, '--fail-on-change'], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            coverage = json.loads(result.stdout)['rule_coverage'] if fmt == 'json' else markdown_coverage(result.stdout)
            for side in ('before', 'after'):
                self.assertEqual(coverage[side][0]['matched_nodes'], nodes('/region/aside[0]', '/region/aside[0]/em[0]'))
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            before, after, rule_file, output = [directory / name for name in ('before.html', 'after.html', 'rules.json', 'out.json')]
            good = '<main>Keep<aside><em>N1</em></aside></main>'
            for gate, entry, bad in (
                ('minimum', {'selector': 'em', 'min_matches': 1}, good.replace('<em>N1</em>', 'Noise')),
                ('maximum', {'selector': 'em', 'max_matches': 1}, good.replace('</aside>', '<em>N2</em></aside>'))):
                rule_file.write_text(json.dumps({**BASE, 'ignore_selectors': ['aside', entry]}))
                for a, b, side in ((bad, good, 'before'), (good, bad, 'after'), (bad, bad, 'before')):
                    before.write_text(a); after.write_text(b)
                    originals = [p.read_bytes() for p in (before, after, rule_file)]
                    for extra in ([], ['--format', 'markdown'], ['--dry-run', '--fail-on-change']):
                        for existing in (False, True):
                            output.unlink(missing_ok=True)
                            if existing:
                                output.write_bytes(b'previous report\x00\xff')
                            with self.subTest(gate=gate, side=side, extra=extra, existing=existing):
                                result = subprocess.run([sys.executable, '-m', 'qawrench.page_diff', str(before), str(after),
                                                         '--rules', str(rule_file), '--out', str(output), *extra],
                                                        capture_output=True, text=True, timeout=20)
                                self.assertEqual(result.returncode, 2, result.stderr)
                                for detail in (side + ':', 'ignore_selectors[1]', 'expected at least' if gate == 'minimum' else 'expected at most'):
                                    self.assertIn(detail, result.stderr)
                                self.assertEqual(result.stdout, '')
                                self.assertEqual(output.read_bytes() if existing else output.exists(), b'previous report\x00\xff' if existing else False)
                                self.assertEqual([p.read_bytes() for p in (before, after, rule_file)], originals)
