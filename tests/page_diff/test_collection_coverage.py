"""Strict collections expose snapshot-local original evidence, including unchanged items."""
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
FIXTURE = ROOT / 'examples' / 'page_diff' / 'collection-coverage'
RULES = {'version': 1, 'required_region': 'main', 'unordered_collections': [
    {'selector': '.items', 'items': 'article', 'key_attribute': 'data-id'}]}


def located(path, parent, key):
    return {'original_path': path, 'original_parent_path': parent, 'key': key}


def collection_rows(report, side):
    return [r for r in report['rule_coverage'][side] if r['rule_id'].startswith('unordered_collections[')]


def markdown_coverage(text):
    return json.loads(text.split('## Normalization rule coverage\n', 1)[1].split('```json\n', 1)[1].split('\n```', 1)[0])


class CollectionCoverageTests(unittest.TestCase):
    def inventory(self, report, side, index=0):
        row = collection_rows(report, side)[index]
        nodes = row['matched_items']
        self.assertEqual(len(nodes), row['item_count'])
        for node in nodes:
            self.assertEqual(set(node), {'original_path', 'original_parent_path', 'key'})
            self.assertEqual(node['original_parent_path'], row['original_path'])
        return nodes

    def test_identical_two_items_have_original_inventory_despite_no_changes(self):
        rules = load_rules(FIXTURE / 'rules.json')
        original = deepcopy(rules)
        report = compare_files(FIXTURE / 'snapshot.html', FIXTURE / 'snapshot.html', rules)
        self.assertEqual((report['status'], report['changes'], report['suppressed']), ('unchanged', [], []))
        parent = '/region/section[0]'
        for side in ('before', 'after'):
            self.assertEqual(collection_rows(report, side)[0]['original_path'], parent)
            self.assertEqual(self.inventory(report, side), [located(parent+'/article[0]', parent, 'Z'),
                                                          located(parent+'/article[1]', parent, 'A')])
            audit = report['normalizations'][side]
            self.assertEqual(len(audit), 1)
            self.assertEqual((audit[0]['original_path'], audit[0]['old'], audit[0]['new']), (parent, ['Z', 'A'], ['A', 'Z']))
        self.assertEqual(markdown_coverage(markdown(report)), report['rule_coverage'])
        self.assertEqual(rules, original)

    def test_original_dom_order_is_not_selector_key_or_lexicographic_path_order(self):
        rules = {**RULES, 'unordered_collections': [
            {'selector': '.items', 'items': '.last, .first', 'key_attribute': 'data-id'}]}
        items = [f'<article class="{("first" if i % 2 == 0 else "last")}" data-id="{12-i}">Item</article>' for i in range(12)]
        def page(parts):
            return '<main><section class="items">' + ''.join(parts) + '</section></main>'
        report = compare_html(page(items), page(items[::-1]), rules)
        parent = '/region/section[0]'
        for side, keys in (('before', range(12, 0, -1)), ('after', range(1, 13))):
            self.assertEqual(self.inventory(report, side), [located(f'{parent}/article[{i}]', parent, str(key)) for i, key in enumerate(keys)])
        self.assertEqual((report['status'], report['changes']), ('unchanged', []))
        self.assertEqual([s['rule'] for s in report['suppressed']], ['unordered_collection'])

    def test_rule_container_and_nested_identity_scopes_keep_pre_ignore_positions(self):
        key = ' z/a~"<`é '
        rules = {**RULES, 'ignore_selectors': ['.noise'], 'normalize_whitespace': True,
                 'volatile_attributes': [{'selector': 'article', 'attributes': ['title']}],
                 'unordered_collections': [
                     {'selector': '.one', 'items': 'article', 'key_attribute': 'data-id'},
                     {'selector': '.two', 'items': 'article', 'key_attribute': 'data-id'}],
                 'sibling_identity': [{'selector': '.one, .two', 'key_attribute': 'class'},
                                      {'selector': 'p[id]', 'key_attribute': 'id'}]}
        def container(cls, reverse=False, changed=False):
            a = '<article data-id=" z/a~&quot;&lt;`é " title="old"><p class="noise">Noise</p><p id="X">' + ('11' if changed else '10') + '</p></article>'
            b = '<article data-id="A"><p id="X">20</p></article>'
            return f'<section class="{cls}">' + (b+a if reverse else a+b) + '</section>'
        a = '<main><section class="noise">Noise</section>' + container('one') + container('two') + '</main>'
        b = '<main><section class="noise">Noise</section>' + container('two', True) + container('one', True, True) + '</main>'
        report = compare_html(a, b, rules)
        for side, indices, keys in (('before', (1, 2), [key, 'A']), ('after', (2, 1), ['A', key])):
            for rule, position in enumerate(indices):
                parent = f'/region/section[{position}]'
                self.assertEqual(self.inventory(report, side, rule), [located(f'{parent}/article[{i}]', parent, k) for i, k in enumerate(keys)])
            identity = next(r for r in report['rule_coverage'][side] if r['rule_id'] == 'sibling_identity[1]')
            expected = []
            for section in (1, 2):
                for i, k in enumerate(keys):
                    parent = f'/region/section[{section}]/article[{i}]'
                    expected.append(located(parent+('/p[1]' if k == key else '/p[0]'), parent, 'X'))
            self.assertEqual(identity['matched_nodes'], expected)
        edits = [c for c in report['changes'] if c['kind'] == 'changed']
        self.assertEqual([(c['old'], c['new']) for c in edits], [('10', '11')])
        self.assertEqual((edits[0]['before_path'], edits[0]['after_path']),
                         ('/region/section[1]/article[0]/p[1]/#text[0]', '/region/section[2]/article[1]/p[1]/#text[0]'))
        self.assertIn('z~1a~0', edits[0]['path'])
        output = markdown(report)
        self.assertEqual(markdown_coverage(output), report['rule_coverage'])
        self.assertNotIn('<`é', output)
        self.assertIn('\\u003c\\u0060\\u00e9', output)

    def test_empty_collections_and_region_root_containers(self):
        empty = '<main><h1>Empty catalog</h1><section class="items"></section></main>'
        item = empty.replace('</section>', '<article data-id="A">Item</article></section>')
        for a, b in ((empty, empty), (empty, item), (item, empty)):
            report = compare_html(a, b, RULES)
            for side, html in (('before', a), ('after', b)):
                self.assertEqual(collection_rows(report, side)[0]['original_path'], '/region/section[0]')
                self.assertEqual(self.inventory(report, side), [] if html == empty else [located('/region/section[0]/article[0]', '/region/section[0]', 'A')])
        rules = {**RULES, 'unordered_collections': [{**RULES['unordered_collections'][0], 'selector': 'main'}]}
        html = '<main><article data-id="  A  ">Item</article></main>'
        report = compare_html(html, html, rules)
        self.assertEqual(collection_rows(report, 'before')[0]['original_path'], '/region')
        self.assertEqual(self.inventory(report, 'before'), [located('/region/article[0]', '/region', '  A  ')])

    def test_unobserved_items_have_no_counterpart_claim(self):
        a = '<main><section class="items"><article data-id="old">10</article></section></main>'
        b = a.replace('old', 'new')
        report = compare_html(a, b, RULES)
        for side, key in (('before', 'old'), ('after', 'new')):
            self.assertEqual(self.inventory(report, side), [located('/region/section[0]/article[0]', '/region/section[0]', key)])
        self.assertEqual({c['kind'] for c in report['changes']}, {'added', 'removed'})

    def test_strict_invalid_collection_contracts_are_unchanged(self):
        bodies = [('<article>Item</article>', 'missing/empty item key'),
                  ('<article data-id="">Item</article>', 'missing/empty item key'),
                  ('<article data-id="&#x2003;">Item</article>', 'missing/empty item key'),
                  ('<article data-id="A">One</article><article data-id="A">Two</article>', 'duplicate item key'),
                  ('<div><article data-id="A">Item</article></div>', 'direct children'),
                  ('<h2>Heading</h2><article data-id="A">Item</article>', 'non-item content'),
                  ('<!--Comment--><article data-id="A">Item</article>', 'non-item content'),
                  ('Stray<article data-id="A">Item</article>', 'non-item content')]
        good = (FIXTURE / 'snapshot.html').read_text()
        for body, error in bodies:
            bad = '<main><h1>Catalog</h1><section class="items">' + body + '</section></main>'
            for a, b, side in ((bad, good, 'before'), (good, bad, 'after')):
                with self.subTest(body=body, side=side), self.assertRaisesRegex(ContractError, side+':.*'+error):
                    compare_html(a, b, RULES)
        for update, error in (({'ignore_selectors': ['article']}, 'ignore selector removes'),
                              ({'volatile_attributes': [{'selector': 'article', 'attributes': ['data-id']}]}, 'cannot normalize item key'),
                              ({'ignore_selectors': [{'selector': '.absent'}]}, 'normalization coverage failed'),
                              ({'volatile_attributes': [{'selector': 'article', 'attributes': ['title'], 'max_matches': 1}]}, 'normalization coverage failed')):
            with self.subTest(update=update), self.assertRaisesRegex(ContractError, error):
                compare_html(good, good, {**RULES, **update})

    def test_other_coverage_kinds_do_not_gain_collection_fields(self):
        rules = {'version': 1, 'required_region': 'main', 'ignore_selectors': ['aside'],
                 'volatile_attributes': [{'selector': 'p', 'attributes': ['title']}],
                 'sibling_identity': [{'selector': 'p', 'key_attribute': 'id'}],
                 'required_attributes': [{'selector': 'p', 'attributes': ['id']}]}
        html = '<main><aside>Ignore</aside><p id="A">Keep</p></main>'
        report = compare_html(html, html, rules)
        for side in ('before', 'after'):
            for row in report['rule_coverage'][side]:
                self.assertNotIn('original_path', row)
                self.assertNotIn('matched_items', row)

    def test_cli_json_markdown_and_invalid_collection_or_coverage_no_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            def cli(before, after, rules, *extra):
                return subprocess.run([sys.executable, '-m', 'qawrench.page_diff', str(before), str(after), '--rules', str(rules), *map(str, extra)],
                                      cwd=ROOT, text=True, capture_output=True, timeout=20)
            args = (FIXTURE / 'snapshot.html', FIXTURE / 'snapshot.html', FIXTURE / 'rules.json')
            result = cli(*args, '--fail-on-change')
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(len(self.inventory(report, 'before')), 2)
            result = cli(*args, '--format', 'markdown', '--fail-on-change')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(markdown_coverage(result.stdout), report['rule_coverage'])
            bad, rules, out = (directory / name for name in ('bad.html', 'rules.json', 'out.json'))
            cases = [('<main><section class="items"><article data-id="D">1</article><article data-id="D">2</article></section></main>', RULES, 'duplicate item key'),
                     ((FIXTURE / 'snapshot.html').read_text(), {**RULES, 'ignore_selectors': ['.absent']}, 'normalization coverage failed')]
            for html, config, error in cases:
                bad.write_text(html)
                rules.write_text(json.dumps(config))
                for extra in ((), ('--format', 'markdown', '--fail-on-change'), ('--dry-run', '--fail-on-change')):
                    for existing in (False, True):
                        out.unlink(missing_ok=True)
                        if existing:
                            out.write_text('previous report')
                        result = cli(bad, bad, rules, '--out', out, *extra)
                        self.assertEqual(result.returncode, 2, result.stderr)
                        self.assertIn(error, result.stderr)
                        self.assertEqual(result.stdout, '')
                        self.assertEqual(out.read_text(), 'previous report') if existing else self.assertFalse(out.exists())
