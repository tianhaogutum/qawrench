"""Snapshot-local eligible text inventories before keyed sorting or empty removal."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from qawrench.page_diff import ContractError, compare_files, compare_html, load_rules
from qawrench.page_diff.cli import markdown

FIXTURE = Path(__file__).resolve().parents[2] / 'examples' / 'page_diff' / 'whitespace-coverage'
BASE = {'version': 1, 'required_region': 'main', 'normalize_whitespace': True}


def nodes(*paths):
    return [{'original_path': p} for p in paths]


def row(report, side):
    return next(r for r in report['rule_coverage'][side] if r['rule_id'] == 'normalize_whitespace')


def markdown_coverage(text):
    return json.loads(text.split('## Normalization rule coverage\n', 1)[1].split('```json\n', 1)[1].split('\n```', 1)[0])


class WhitespaceCoverageTests(unittest.TestCase):
    def inventory(self, report, side):
        coverage = row(report, side)
        audit = [e for e in report['normalizations'][side] if e['rule'] == 'normalize_whitespace']
        self.assertEqual(coverage['text_nodes'], len(coverage['matched_nodes']))
        self.assertEqual(coverage['text_nodes'], len(audit))
        self.assertEqual(coverage['changed_text_nodes'], sum(e['old'] != e['new'] for e in audit))
        self.assertTrue(all(set(n) == {'original_path'} for n in coverage['matched_nodes']))
        self.assertCountEqual(coverage['matched_nodes'], nodes(*(e['original_path'] for e in audit)))
        return coverage['matched_nodes']

    def test_identical_inventory_includes_unchanged_and_normalized_empty(self):
        rules = load_rules(FIXTURE / 'rules.json'); original = deepcopy(rules)
        report = compare_files(FIXTURE / 'snapshot.html', FIXTURE / 'snapshot.html', rules)
        self.assertEqual((report['status'], report['changes'], report['suppressed']), ('unchanged', [], []))
        for side in ('before', 'after'):
            coverage = row(report, side)
            self.assertEqual((coverage['text_nodes'], coverage['changed_text_nodes']), (3, 2))
            audit = [e for e in report['normalizations'][side] if e['rule'] == 'normalize_whitespace']
            self.assertEqual([(e['original_path'], e['old'], e['new']) for e in audit], [
                ('/region/#text[0]', ' ', ''), ('/region/p[1]/#text[0]', 'Keep', 'Keep'),
                ('/region/p[2]/#text[0]', ' A  B ', 'A B')])
            self.assertEqual(coverage['matched_nodes'], nodes('/region/#text[0]', '/region/p[1]/#text[0]', '/region/p[2]/#text[0]'))
            self.inventory(report, side)
        self.assertEqual(markdown_coverage(markdown(report)), report['rule_coverage'])
        self.assertEqual(rules, original)

    def test_eligibility_excludes_ignored_protected_ancestors_and_string_subclasses(self):
        page = '<main> Keep <!-- A  B --><p class="noise">Ignored <i>nested</i></p>'
        page += ''.join(f'<{tag}> X  Y <b> Z  Q </b></{tag}>' for tag in ('pre', 'code', 'textarea', 'script', 'style'))
        page += '<p> A\u2003 B </p> </main>'
        report = compare_html(page, page, {**BASE, 'ignore_selectors': ['.noise']})
        expected = nodes('/region/#text[0]', '/region/p[1]/#text[0]', '/region/#text[1]')
        for side in ('before', 'after'):
            self.assertEqual(self.inventory(report, side), expected)
            self.assertEqual(row(report, side)['changed_text_nodes'], 3)
        changed = compare_html(page, page.replace('<!-- A  B -->', '<!--A B-->').replace(' X  Y ', 'X Y'),
                               {**BASE, 'ignore_selectors': ['.noise']})
        self.assertEqual(changed['status'], 'changed')
        self.assertEqual(len(changed['changes']), 6)
        # Ancestors outside the selected region also protect its text.
        for tag in ('pre', 'code'):
            outer = f'<{tag}><main> A  B </main></{tag}>'
            protected = compare_html(outer, outer, BASE)
            self.assertEqual(self.inventory(protected, 'before'), [])

    def test_one_sided_normalized_empty_node_has_independent_inventory(self):
        before = '<main><p>Keep</p> \n </main>'
        after = '<main><p>Keep</p></main>'
        report = compare_html(before, after, BASE)
        self.assertEqual(self.inventory(report, 'before'), nodes('/region/p[0]/#text[0]', '/region/#text[0]'))
        self.assertEqual(self.inventory(report, 'after'), nodes('/region/p[0]/#text[0]'))
        self.assertEqual((row(report, 'before')['changed_text_nodes'], row(report, 'after')['changed_text_nodes']), (1, 0))
        self.assertEqual(report['changes'], [])
        self.assertEqual(len(report['suppressed']), 1)
        suppression = report['suppressed'][0]
        self.assertEqual((suppression['old'], suppression['new'], suppression['normalized']), (' \n ', None, ''))
        self.assertEqual((suppression['before_path'], suppression['after_path']), ('/region/#text[0]', None))
        self.assertEqual(markdown_coverage(markdown(report)), report['rule_coverage'])

    def test_zero_eligible_and_disabled(self):
        page = '<main><code>Protected ordinary text</code><!--comment--></main>'
        report = compare_html(page, page, BASE)
        for side in ('before', 'after'):
            self.assertEqual(row(report, side), {'rule_id': 'normalize_whitespace', 'text_nodes': 0,
                                               'changed_text_nodes': 0, 'matched_nodes': []})
            self.inventory(report, side)
        for rules in ({'version': 1, 'required_region': 'main'}, {**BASE, 'normalize_whitespace': False}):
            disabled = compare_html(page, page, rules)
            self.assertEqual(disabled['rule_coverage']['before'], [])
            self.assertEqual(disabled['normalizations']['before'], [])

    def test_collection_dom_preorder_separators_and_nested_identities(self):
        rules = {**BASE, 'ignore_selectors': ['.noise'],
                 'unordered_collections': [{'selector': 'section', 'items': 'article', 'key_attribute': 'id'}],
                 'sibling_identity': [{'selector': 'p.keep', 'key_attribute': 'id'}],
                 'required_attributes': [{'selector': 'article', 'attributes': ['id']}],
                 'volatile_attributes': [{'selector': 'article', 'attributes': ['title']}]}
        def item(key):
            return f'<article id="{key}" title="x"> <p class="noise">Gone</p><p class="keep" id="K"> {key}  Keep </p> </article>'
        before = '<main><section> '+item('Z')+'\n'+item('A')+' </section></main>'
        after = '<main><section> '+item('A')+'\n'+item('Z')+' </section></main>'
        report = compare_html(before, after, rules)
        prefix = '/region/section[0]'
        expected = nodes(prefix+'/#text[0]', prefix+'/article[0]/#text[0]', prefix+'/article[0]/p[1]/#text[0]',
                         prefix+'/article[0]/#text[1]', prefix+'/#text[1]', prefix+'/article[1]/#text[0]',
                         prefix+'/article[1]/p[1]/#text[0]', prefix+'/article[1]/#text[1]', prefix+'/#text[2]')
        for side in ('before', 'after'):
            self.assertEqual(self.inventory(report, side), expected)
            self.assertEqual(row(report, side)['changed_text_nodes'], 9)
            collection = report['rule_coverage'][side][2]
            self.assertEqual([n['key'] for n in collection['matched_items']], ['Z', 'A'] if side == 'before' else ['A', 'Z'])
        # Existing audit traverses sorted keys; coverage must follow original DOM instead.
        audit = [e['original_path'] for e in report['normalizations']['before'] if e['rule'] == 'normalize_whitespace']
        self.assertEqual(audit[0], prefix+'/article[1]/#text[0]')
        self.assertNotEqual(nodes(*audit), expected)
        self.assertEqual(report['changes'], [])
        self.assertEqual([s['rule'] for s in report['suppressed']], ['unordered_collection'])

    def test_dom_order_beyond_ten_and_original_text_indices(self):
        page = '<main> a <i class="noise">Gone</i> b <!--marker--> c '
        page += ''.join(f'<p>{i}<span>nested</span> </p>' for i in range(12)) + '</main>'
        report = compare_html(page, page, {**BASE, 'ignore_selectors': ['.noise']})
        expected = ['/region/#text[0]', '/region/#text[1]', '/region/#text[2]']
        expected += [p for i in range(12) for p in (f'/region/p[{i}]/#text[0]', f'/region/p[{i}]/span[0]/#text[0]', f'/region/p[{i}]/#text[1]')]
        self.assertEqual(self.inventory(report, 'before'), nodes(*expected))

    def test_reordered_and_ambiguous_sibling_identities_retain_semantic_and_suppression_locations(self):
        rules = {**BASE, 'sibling_identity': [{'selector': 'article', 'key_attribute': 'id'}]}
        a, b = '<article id="A"> A  B </article>', '<article id="B">Keep</article>'
        report = compare_html('<main>'+a+b+'</main>', '<main>'+b+a.replace(' A  B ', 'A C')+'</main>', rules)
        for side in ('before', 'after'):
            self.assertEqual(self.inventory(report, side), nodes('/region/article[0]/#text[0]', '/region/article[1]/#text[0]'))
        change = next(c for c in report['changes'] if c['kind'] == 'changed')
        self.assertEqual((change['old'], change['new'], change['before_path'], change['after_path']),
                         ('A B', 'A C', '/region/article[0]/#text[0]', '/region/article[1]/#text[0]'))
        page = '<main><article id="X"> A  B </article><article id="X"> C  D </article><article id="Y"> E  F </article></main>'
        ambiguous = compare_html(page, page.replace('  ', ' '), rules)
        for side in ('before', 'after'):
            self.assertEqual(self.inventory(ambiguous, side), nodes(*(f'/region/article[{i}]/#text[0]' for i in range(3))))
        self.assertTrue(any(c['kind'] == 'ambiguous' for c in ambiguous['changes']))
        self.assertEqual(len(ambiguous['suppressed']), 1)
        self.assertEqual(ambiguous['suppressed'][0]['before_path'], '/region/article[2]/#text[0]')

    def test_cli_json_markdown_dry_run_and_invalid_no_report(self):
        command = [sys.executable, '-m', 'qawrench.page_diff', str(FIXTURE/'snapshot.html'),
                   str(FIXTURE/'snapshot.html'), '--rules', str(FIXTURE/'rules.json')]
        for fmt in ('json', 'markdown'):
            result = subprocess.run(command+['--format', fmt, '--fail-on-change'], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            coverage = json.loads(result.stdout)['rule_coverage'] if fmt == 'json' else markdown_coverage(result.stdout)
            self.assertEqual(coverage['before'][-1]['matched_nodes'], nodes('/region/#text[0]', '/region/p[1]/#text[0]', '/region/p[2]/#text[0]'))
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            before, after, config, out = [directory/n for n in ('before.html', 'after.html', 'rules.json', 'out')]
            good = '<main><p>Keep</p></main>'
            for bad, rules, diagnostic in (
                (good, {**BASE, 'normalize_whitespace': 'true'}, 'must be boolean'),
                ('<main> </main>', BASE, 'no text content'),
                (good, {**BASE, 'required_attributes': [{'selector': 'p', 'attributes': ['id']}]}, 'required attribute'),
                ('<main><section><p id="X">One</p><p id="X">Two</p></section></main>',
                 {**BASE, 'unordered_collections': [{'selector': 'section', 'items': 'p', 'key_attribute': 'id'}]}, 'duplicate item key')):
                valid = good if 'unordered_collections' not in rules else bad.replace('id="X">Two', 'id="Y">Two')
                for first, second in ((bad, valid), (valid, bad)):
                    before.write_text(first); after.write_text(second); config.write_text(json.dumps(rules))
                    for extra in ([], ['--format', 'markdown', '--fail-on-change'], ['--dry-run', '--fail-on-change']):
                        for existing in (False, True):
                            out.unlink(missing_ok=True)
                            if existing: out.write_bytes(b'previous\x00\xff')
                            r = subprocess.run([sys.executable, '-m', 'qawrench.page_diff', str(before), str(after), '--rules', str(config), '--out', str(out), *extra], capture_output=True, text=True, timeout=20)
                            self.assertEqual(r.returncode, 2, r.stderr)
                            self.assertIn(diagnostic, r.stderr)
                            self.assertEqual(r.stdout, '')
                            self.assertEqual(out.read_bytes() if existing else out.exists(), b'previous\x00\xff' if existing else False)
                            self.assertFalse(list(directory.glob('.stable-diff-*')))
