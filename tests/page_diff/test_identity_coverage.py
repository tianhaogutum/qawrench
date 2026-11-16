"""Original identity candidates are evidence even without changes or audit events."""
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
EXAMPLES = ROOT / 'examples' / 'page_diff'
FIXTURE = EXAMPLES / 'identity-coverage'
RULES = {'version': 1, 'required_region': 'main', 'sibling_identity': [
    {'selector': 'article', 'key_attribute': 'data-id'}]}


def located(path, parent, key):
    return {'original_path': path, 'original_parent_path': parent, 'key': key}


def identity_rows(report, side):
    return [r for r in report['rule_coverage'][side] if r['rule_id'].startswith('sibling_identity[')]


def markdown_coverage(text):
    return json.loads(text.split('## Normalization rule coverage\n', 1)[1].split('```json\n', 1)[1].split('\n```', 1)[0])


class IdentityCoverageTests(unittest.TestCase):
    def inventory(self, report, side, index=0):
        row = identity_rows(report, side)[index]
        nodes = row['matched_nodes']
        self.assertEqual(len(nodes), row['matched_elements'])
        for node in nodes:
            self.assertEqual(set(node), {'original_path', 'original_parent_path', 'key'})
        return nodes

    def test_identical_two_cards_have_located_evidence_without_changes_or_audit(self):
        rules = load_rules(FIXTURE / 'rules.json')
        original = deepcopy(rules)
        report = compare_files(FIXTURE / 'snapshot.html', FIXTURE / 'snapshot.html', rules)
        self.assertEqual((report['status'], report['changes'], report['suppressed']), ('unchanged', [], []))
        self.assertEqual(report['normalizations'], {'before': [], 'after': []})
        for side in ('before', 'after'):
            self.assertEqual(self.inventory(report, side), [located('/region/article[0]', '/region', 'A'),
                                                          located('/region/article[1]', '/region', 'B')])
        self.assertEqual(rules, original)

    def test_preorder_includes_nested_matches_and_double_digit_indices(self):
        # Selector-list order, key order and lexicographic path order differ from preorder.
        rules = {**RULES, 'sibling_identity': [{'selector': 'article.inner, article.outer', 'key_attribute': 'data-id'}]}
        children = ''.join(f'<article class="inner" data-id="{12-i}">Item</article>' for i in range(12))
        html = '<main><article class="outer" data-id="Z">Parent' + children + '</article></main>'
        report = compare_html(html, html, rules)
        expected = [located('/region/article[0]', '/region', 'Z')] + [
            located(f'/region/article[0]/article[{i}]', '/region/article[0]', str(12-i)) for i in range(12)]
        for side in ('before', 'after'):
            self.assertEqual(self.inventory(report, side), expected)
        self.assertEqual(report['changes'], [])

    def test_reordered_paired_parents_keep_independent_parent_and_rule_scopes(self):
        rules = {**RULES, 'sibling_identity': [
            {'selector': 'section', 'key_attribute': 'id'},
            {'selector': '.one', 'key_attribute': 'data-id'},
            {'selector': '.two', 'key_attribute': 'data-id'}]}
        a = '<section id="A"><article class="one" data-id="a/b~&quot;&lt;`é">10</article><article class="two" data-id="a/b~&quot;&lt;`é">20</article></section>'
        b = a.replace('id="A"', 'id="B"')
        report = compare_html('<main>' + a + b + '</main>', '<main>' + b + a.replace('>10<', '>11<') + '</main>', rules)
        key = 'a/b~"<`é'
        for side, keys in (('before', ['A', 'B']), ('after', ['B', 'A'])):
            self.assertEqual(self.inventory(report, side), [located(f'/region/section[{i}]', '/region', k) for i, k in enumerate(keys)])
            for rule, child in ((1, 0), (2, 1)):
                self.assertEqual(self.inventory(report, side, rule), [
                    located(f'/region/section[{i}]/article[{child}]', f'/region/section[{i}]', key) for i in range(2)])
        edits = [c for c in report['changes'] if c['kind'] == 'changed']
        self.assertEqual([(c['old'], c['new']) for c in edits], [('10', '11')])
        self.assertIn('a~1b~0', edits[0]['path'])
        self.assertEqual((edits[0]['before_path'], edits[0]['after_path']),
                         ('/region/section[0]/article[0]/#text[0]', '/region/section[1]/article[0]/#text[0]'))
        self.assertFalse(any(c['kind'] == 'ambiguous' for c in report['changes']))
        output = markdown(report)
        self.assertEqual(markdown_coverage(output), report['rule_coverage'])
        self.assertNotIn('<`é', output)
        self.assertIn('\\u003c\\u0060\\u00e9', output)

    def test_positions_precede_ignore_whitespace_volatile_and_collection_sorting(self):
        directory = EXAMPLES / 'suppression-targets'
        report = compare_files(directory / 'before.html', directory / 'after.html', load_rules(directory / 'rules.json'))
        for side, keys in (('before', ['A', 'B']), ('after', ['B', 'A'])):
            self.assertEqual(self.inventory(report, side), [located(f'/region/article[{i}]', '/region', k) for i, k in enumerate(keys, 1)])
        for side in ('before', 'after'):
            events = [e for e in report['normalizations'][side] if e['rule'] == 'volatile_attributes']
            self.assertEqual([e['original_target_path'] for e in events], [n['original_path'] for n in self.inventory(report, side)])
        rules = {**RULES, 'unordered_collections': [{'selector': 'section', 'items': 'div', 'key_attribute': 'id'}]}
        html = '<main><section><div id="Z"><article data-id="last">Z</article></div><div id="A"><article data-id="first">A</article></div></section></main>'
        report = compare_html(html, html, rules)
        self.assertEqual(self.inventory(report, 'before'), [
            located('/region/section[0]/div[0]/article[0]', '/region/section[0]/div[0]', 'last'),
            located('/region/section[0]/div[1]/article[0]', '/region/section[0]/div[1]', 'first')])
        self.assertNotIn('matched_nodes', report['rule_coverage']['before'][0])

    def test_duplicate_missing_and_blank_candidates_are_individual_not_pairings(self):
        rules = {**RULES, 'volatile_attributes': [{'selector': 'article', 'attributes': ['title']}]}
        before = '<main><article data-id="D">1</article><article data-id="D">2</article><article>3</article><article data-id="">4</article><article data-id="  ">5</article><article data-id=" A ">6</article></main>'
        after = '<main><article data-id="D" title="new">1</article></main>'
        for a, b in ((before, before), (before, after), (after, before)):
            with self.subTest(a=a, b=b):
                report = compare_html(a, b, rules)
                for side, html in (('before', a), ('after', b)):
                    keys = ['D', 'D', None, None, None, ' A '] if html == before else ['D']
                    self.assertEqual(self.inventory(report, side), [located(f'/region/article[{i}]', '/region', k) for i, k in enumerate(keys)])
                self.assertEqual(report['status'], 'changed')
                self.assertTrue(any(c['reason'] == 'duplicate_key' for c in report['changes'] if c['kind'] == 'ambiguous'))
                self.assertEqual(report['suppressed'], [])

    def test_zero_and_one_sided_selector_membership_have_independent_arrays(self):
        rules = {**RULES, 'sibling_identity': [{'selector': '.selected', 'key_attribute': 'data-id'},
                                            {'selector': '.absent', 'key_attribute': 'id'}]}
        plain = '<main><article data-id="A">One</article></main>'
        selected = plain.replace('<article ', '<article class="selected" ')
        for a, b in ((plain, plain), (plain, selected), (selected, plain)):
            report = compare_html(a, b, rules)
            for side, html in (('before', a), ('after', b)):
                self.assertEqual(self.inventory(report, side), [] if html == plain else [located('/region/article[0]', '/region', 'A')])
                self.assertEqual(self.inventory(report, side, 1), [])

    def test_invalid_identity_contracts_and_nested_coverage_still_fail(self):
        html = '<main><article data-id="A"><aside class="noise"><span class="noise">Noise</span></aside>Keep</article></main>'
        for update in ({'sibling_identity': [{'selector': 'main', 'key_attribute': 'id'}]},
                       {'sibling_identity': RULES['sibling_identity'] * 2},
                       {'ignore_selectors': ['article']},
                       {'volatile_attributes': [{'selector': 'article', 'attributes': ['data-id']}]},
                       {'ignore_selectors': [{'selector': '.noise', 'max_matches': 1}]},
                       {'volatile_attributes': [{'selector': '.noise', 'attributes': ['title'], 'max_matches': 1}]}):
            with self.subTest(update=update), self.assertRaises(ContractError):
                compare_html(html, html, {**RULES, **update})

    def test_cli_json_markdown_and_nested_gate_no_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            def cli(before, after, rules, *extra):
                return subprocess.run([sys.executable, '-m', 'qawrench.page_diff', str(before), str(after),
                                       '--rules', str(rules), *map(str, extra)], cwd=ROOT, text=True,
                                      capture_output=True, timeout=20)
            args = (FIXTURE / 'snapshot.html', FIXTURE / 'snapshot.html', FIXTURE / 'rules.json')
            result = cli(*args, '--fail-on-change')
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(len(self.inventory(report, 'before')), 2)
            result = cli(*args, '--format', 'markdown', '--fail-on-change')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(markdown_coverage(result.stdout), report['rule_coverage'])
            html = directory / 'nested.html'
            html.write_text('<main><article data-id="A"><aside class="noise"><span class="noise">Noise</span></aside>Keep</article></main>')
            rules = directory / 'rules.json'
            out = directory / 'out.json'
            for field, entry in (('ignore_selectors', {'selector': '.noise', 'max_matches': 1}),
                                 ('volatile_attributes', {'selector': '.noise', 'attributes': ['title'], 'max_matches': 1})):
                rules.write_text(json.dumps({**RULES, field: [entry]}))
                for extra in ((), ('--format', 'markdown', '--fail-on-change'), ('--dry-run', '--fail-on-change')):
                    for existing in (False, True):
                        out.unlink(missing_ok=True)
                        if existing:
                            out.write_text('previous report')
                        result = cli(html, html, rules, '--out', out, *extra)
                        self.assertEqual(result.returncode, 2, result.stderr)
                        self.assertIn('expected at most 1 matched elements, got 2', result.stderr)
                        self.assertEqual(result.stdout, '')
                        self.assertEqual(out.read_text(), 'previous report') if existing else self.assertFalse(out.exists())
