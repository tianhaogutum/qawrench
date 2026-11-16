"""Per-rule surviving volatile targets, independent of attribute occurrences."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from qawrench.page_diff import ContractError, compare_files, compare_html, load_rules
from qawrench.page_diff.cli import markdown

FIXTURE = Path(__file__).resolve().parents[2] / 'examples' / 'page_diff' / 'volatile-coverage'
BASE = {'version': 1, 'required_region': 'main'}


def nodes(*paths):
    return [{'original_path': p} for p in paths]


def rows(report, side):
    return [r for r in report['rule_coverage'][side] if r['rule_id'].startswith('volatile_attributes[')]


def markdown_coverage(text):
    return json.loads(text.split('## Normalization rule coverage\n', 1)[1].split('```json\n', 1)[1].split('\n```', 1)[0])


class VolatileCoverageTests(unittest.TestCase):
    def inventories(self, report, side):
        selected = rows(report, side)
        for row in selected:
            self.assertEqual(len(row['matched_nodes']), row['matched_elements'])
            self.assertTrue(all(set(n) == {'original_path'} for n in row['matched_nodes']))
            # Multiple attributes produce multiple events, never multiple inventory entries.
            audit_paths = {e['original_target_path'] for e in report['normalizations'][side]
                          if e['rule'] == 'volatile_attributes' and
                          any(r['rule_id'] == row['rule_id'] for r in e['rules'])}
            self.assertEqual(audit_paths, {n['original_path'] for n in row['matched_nodes']})
        return [r['matched_nodes'] for r in selected]

    def test_identical_missing_empty_targets_have_per_rule_inventory(self):
        rules = load_rules(FIXTURE / 'rules.json')
        original = deepcopy(rules)
        report = compare_files(FIXTURE / 'snapshot.html', FIXTURE / 'snapshot.html', rules)
        self.assertEqual((report['status'], report['changes'], report['suppressed']), ('unchanged', [], []))
        for side in ('before', 'after'):
            row = rows(report, side)[0]
            self.assertEqual(row['matched_elements'], 2)
            self.assertEqual(row['attributes_present'], {'data-render': 1})
            audit = [e for e in report['normalizations'][side] if e['rule'] == 'volatile_attributes']
            self.assertEqual([e['original_target_path'] for e in audit], ['/region/p[1]', '/region/p[2]'])
            self.assertEqual([e['old'] for e in audit], [None, ''])
            self.assertEqual(row['matched_nodes'], nodes('/region/p[1]', '/region/p[2]'))
            self.assertEqual(self.inventories(report, side), [nodes('/region/p[1]', '/region/p[2]')])
        self.assertEqual(markdown_coverage(markdown(report)), report['rule_coverage'])
        self.assertEqual(rules, original)

    def test_dom_preorder_overlap_repetition_zero_and_pre_removal_membership(self):
        selectors = ['strong', 'p, strong', '[data-render]', '[data-render]', '.absent']
        rules = {**BASE, 'volatile_attributes': [
            {'selector': s, 'attributes': ['data-render', 'title'], 'min_matches': 0} for s in selectors]}
        page = '<main>Keep' + ''.join(f'<p data-render="{i}"><strong title="">Nested</strong></p>' for i in range(12)) + '</main>'
        report = compare_html(page, page, rules)
        ps = [f'/region/p[{i}]' for i in range(12)]
        strongs = [p + '/strong[0]' for p in ps]
        preorder = [p for pair in zip(ps, strongs) for p in pair]
        for side in ('before', 'after'):
            self.assertEqual([r['rule_id'] for r in rows(report, side)], [f'volatile_attributes[{i}]' for i in range(5)])
            self.assertEqual(self.inventories(report, side), [nodes(*strongs), nodes(*preorder), nodes(*ps), nodes(*ps), []])
            self.assertEqual([r['attributes_present'] for r in rows(report, side)],
                             [{'data-render': 0, 'title': 12}, {'data-render': 12, 'title': 12},
                              {'data-render': 12, 'title': 0}, {'data-render': 12, 'title': 0}, {'data-render': 0, 'title': 0}])
            events = [e for e in report['normalizations'][side] if e['rule'] == 'volatile_attributes']
            self.assertEqual(len(events), 48)
            self.assertEqual(len({e['original_path'] for e in events}), 48)
        self.assertEqual(report['suppressed'], [])

    def test_post_ignore_css_membership_and_region_root(self):
        rules = {**BASE, 'ignore_selectors': ['.noise'], 'volatile_attributes': [
            {'selector': 'main, p:first-child', 'attributes': ['title']},
            {'selector': '.noise', 'attributes': ['title'], 'min_matches': 0, 'max_matches': 0}]}
        page = '<main title="">\n<p class="noise">Gone</p><p title="x">Keep</p><p>Other</p></main>'
        report = compare_html(page, page, rules)
        for side in ('before', 'after'):
            self.assertEqual(self.inventories(report, side), [nodes('/region', '/region/p[1]'), []])
            self.assertEqual(rows(report, side)[0]['attributes_present'], {'title': 2})
            self.assertEqual(report['rule_coverage'][side][0]['matched_nodes'], nodes('/region/p[0]'))

    def test_reordered_identities_one_sided_membership_and_ambiguity(self):
        rules = {**BASE, 'ignore_selectors': ['.noise'],
                 'sibling_identity': [{'selector': 'article', 'key_attribute': 'id'}],
                 'volatile_attributes': [{'selector': '.old', 'attributes': ['data-render'], 'min_matches': 0},
                                         {'selector': '.new', 'attributes': ['data-render'], 'min_matches': 0}]}
        # Ignore only an inner same-tag sibling; identities cannot themselves be ignored.
        a = '<article id="A"><p class="noise">Gone</p><p class="old" data-render="old">10</p></article>'
        b = '<article id="B"><p>20</p></article>'
        before, after = '<main>'+a+b+'</main>', '<main>'+b+a.replace('class="old"', 'class="new"').replace('="old"', '="new"').replace('>10<', '>11<')+'</main>'
        report = compare_html(before, after, rules)
        self.assertEqual(self.inventories(report, 'before'), [nodes('/region/article[0]/p[1]'), []])
        self.assertEqual(self.inventories(report, 'after'), [[], nodes('/region/article[1]/p[1]')])
        event = next(s for s in report['suppressed'] if s['rule'] == 'volatile_attributes')
        self.assertEqual((event['old'], event['new'], event['before_target_path'], event['after_target_path']),
                         ('old', 'new', '/region/article[0]/p[1]', '/region/article[1]/p[1]'))
        self.assertEqual(event['before_rules'][0]['rule_id'], 'volatile_attributes[0]')
        self.assertEqual(event['after_rules'][0]['rule_id'], 'volatile_attributes[1]')
        self.assertTrue(any(c['old'] == '10' and c['new'] == '11' for c in report['changes']))
        # Duplicate and missing identities still inventory candidates without suppression claims.
        for key in (' id="A"', ''):
            selected = f'<main><article{key} class="old">One</article><article{key} class="old" data-render="">Two</article></main>'
            unselected = selected.replace('class="old"', 'class="other"')
            for first, second in ((selected, unselected), (unselected, selected)):
                r = compare_html(first, second, {**rules, 'ignore_selectors': []})
                for side, html in (('before', first), ('after', second)):
                    self.assertEqual(self.inventories(r, side),
                                     [nodes('/region/article[0]', '/region/article[1]') if 'class="old"' in html else [], []])
                self.assertEqual(r['suppressed'], [])
                self.assertTrue(all(c['kind'] == 'ambiguous' for c in r['changes']))

    def test_collection_order_required_attributes_and_key_protection(self):
        rules = {**BASE, 'ignore_selectors': ['.noise'],
                 'unordered_collections': [{'selector': 'section', 'items': 'article', 'key_attribute': 'id'}],
                 'required_attributes': [{'selector': 'a', 'attributes': ['href']}],
                 'volatile_attributes': [{'selector': 'article, a', 'attributes': ['title']}]}
        a = '<article id="Z"><a class="noise">Gone</a><a href="/one" title="">Keep</a></article>'
        b = '<article id="A"><a href="/two">Other</a></article>'
        # Scope required attributes to surviving links already carrying href.
        rules['required_attributes'][0]['selector'] = 'a[href]'
        report = compare_html('<main><section>'+a+b+'</section></main>', '<main><section>'+b+a+'</section></main>', rules)
        for side, index in (('before', 0), ('after', 1)):
            paths = ['/region/section[0]/article[0]', '/region/section[0]/article[1]']
            self.assertEqual(self.inventories(report, side), [nodes(paths[0], paths[0] + f'/a[{1 if index == 0 else 0}]',
                                                                   paths[1], paths[1] + f'/a[{1 if index == 1 else 0}]')])
            collection = next(r for r in report['rule_coverage'][side] if r['rule_id'] == 'unordered_collections[0]')
            self.assertEqual([n['key'] for n in collection['matched_items']], ['Z', 'A'] if side == 'before' else ['A', 'Z'])
            required = next(r for r in report['rule_coverage'][side] if r['rule_id'] == 'required_attributes[0]')
            self.assertEqual(required['after_ignores'], required['after_volatile'])
        self.assertEqual(report['status'], 'unchanged')
        page = '<main><article id="A"><a href="/one">Keep</a></article></main>'
        with self.assertRaisesRegex(ContractError, 'stage=after_volatile.*missing'):
            compare_html(page, page, {**BASE, 'required_attributes': [{'selector': 'a', 'attributes': ['href']}],
                                      'volatile_attributes': [{'selector': 'a', 'attributes': ['href']}]})
        with self.assertRaisesRegex(ContractError, 'cannot normalize sibling identity key'):
            compare_html(page, page, {**BASE, 'sibling_identity': [{'selector': 'article', 'key_attribute': 'id'}],
                                      'volatile_attributes': [{'selector': 'article', 'attributes': ['id']}]})

    def test_markdown_coverage_escapes_selector_without_mutation(self):
        selector = '[data-note="<`é>"]'
        page = '<main><p data-note="&lt;`é&gt;">Keep</p></main>'
        report = compare_html(page, page, {**BASE, 'volatile_attributes': [{'selector': selector, 'attributes': ['title']}]})
        original = deepcopy(report)
        output = markdown(report)
        self.assertEqual(markdown_coverage(output), report['rule_coverage'])
        self.assertEqual(self.inventories(report, 'before'), [nodes('/region/p[0]')])
        self.assertIn('\\u003c\\u0060\\u00e9\\u003e', output)
        self.assertNotIn('<`é>', output)
        self.assertEqual(report, original)

    def test_cli_json_markdown_and_post_ignore_bound_failures(self):
        command = [sys.executable, '-m', 'qawrench.page_diff', str(FIXTURE / 'snapshot.html'),
                   str(FIXTURE / 'snapshot.html'), '--rules', str(FIXTURE / 'rules.json')]
        for fmt in ('json', 'markdown'):
            result = subprocess.run(command + ['--format', fmt, '--fail-on-change'], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            coverage = json.loads(result.stdout)['rule_coverage'] if fmt == 'json' else markdown_coverage(result.stdout)
            for side in ('before', 'after'):
                self.assertEqual(coverage[side][1]['matched_nodes'], nodes('/region/p[1]', '/region/p[2]'))
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            before, after, config, out = [directory / n for n in ('before.html', 'after.html', 'rules.json', 'out')]
            good = '<main>Keep<p class="noise">Ignored</p><p>Missing</p><p title="">Empty</p></main>'
            for gate, entry, bad in (
                ('minimum', {'min_matches': 2}, good.replace('<p>Missing</p>', '')),
                ('maximum', {'max_matches': 2}, good.replace('</main>', '<p>More</p></main>'))):
                config.write_text(json.dumps({**BASE, 'ignore_selectors': ['.noise'], 'volatile_attributes': [
                    {'selector': 'p', 'attributes': ['title'], **entry}]}))
                for a, b, side in ((bad, good, 'before'), (good, bad, 'after')):
                    before.write_text(a); after.write_text(b)
                    originals = [p.read_bytes() for p in (before, after, config)]
                    for extra in ([], ['--format', 'markdown', '--fail-on-change'], ['--dry-run', '--fail-on-change']):
                        for existing in (False, True):
                            out.unlink(missing_ok=True)
                            if existing:
                                out.write_bytes(b'previous\x00\xff')
                            with self.subTest(gate=gate, side=side, extra=extra, existing=existing):
                                r = subprocess.run([sys.executable, '-m', 'qawrench.page_diff', str(before), str(after),
                                                    '--rules', str(config), '--out', str(out), *extra],
                                                   capture_output=True, text=True, timeout=20)
                                self.assertEqual(r.returncode, 2, r.stderr)
                                for detail in (side + ':', 'volatile_attributes[0]', 'expected at least' if gate == 'minimum' else 'expected at most'):
                                    self.assertIn(detail, r.stderr)
                                self.assertEqual(r.stdout, '')
                                self.assertEqual(out.read_bytes() if existing else out.exists(), b'previous\x00\xff' if existing else False)
                                self.assertEqual([p.read_bytes() for p in (before, after, config)], originals)
                                self.assertFalse(list(directory.glob('.stable-diff-*')))
