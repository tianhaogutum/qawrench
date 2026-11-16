"""Required-text coverage inventories observe gates without strengthening them."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from qawrench.page_diff import ContractError, compare_html, compare_files, load_rules
from qawrench.page_diff.cli import markdown

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'examples' / 'page_diff' / 'required-text-coverage'
BASE = {'version': 1, 'required_region': 'main'}
CLI = [sys.executable, '-m', 'qawrench.page_diff']
CLI_CWD = ROOT


def inventory(*paths):
    return {'matched_elements': len(paths), 'matched_nodes': [{'original_path': p} for p in paths]}


def rows(report, side):
    return [r for r in report['rule_coverage'][side] if r['rule_id'].startswith('required_selectors[')]


def markdown_coverage(text):
    return json.loads(text.split('## Normalization rule coverage\n', 1)[1].split('```json\n', 1)[1].split('\n```', 1)[0])


class RequiredTextCoverageTests(unittest.TestCase):
    def assert_stages(self, row, parsed, ignored=None, volatile=None):
        ignored = parsed if ignored is None else ignored
        volatile = ignored if volatile is None else volatile
        self.assertEqual(set(row), {'rule_id', 'selector', 'matched_elements', 'matched_nodes', 'after_ignores', 'after_volatile'})
        self.assertEqual({k: row[k] for k in parsed}, parsed)
        self.assertEqual(row['after_ignores'], ignored)
        self.assertEqual(row['after_volatile'], volatile)
        for stage in (row, row['after_ignores'], row['after_volatile']):
            self.assertEqual(stage['matched_elements'], len(stage['matched_nodes']))
            self.assertTrue(all(set(n) == {'original_path'} for n in stage['matched_nodes']))

    def test_identical_two_match_snapshot_exact_json_and_markdown(self):
        rules = load_rules(FIXTURE/'rules.json'); original = deepcopy(rules)
        report = compare_files(FIXTURE/'snapshot.html', FIXTURE/'snapshot.html', rules)
        found = inventory('/region/p[0]', '/region/p[1]')
        expected = {'rule_id': 'required_selectors[0]', 'selector': 'p', **found,
                    'after_ignores': found, 'after_volatile': found}
        for side in ('before', 'after'):
            self.assertEqual(report['rule_coverage'][side], [expected])
            self.assert_stages(rows(report, side)[0], found)
        self.assertEqual((report['status'], report['changes'], report['suppressed']), ('unchanged', [], []))
        self.assertEqual(report['normalizations'], {'before': [], 'after': []})
        self.assertEqual(markdown_coverage(markdown(report)), report['rule_coverage'])
        self.assertEqual(rules, original)

    def test_repeated_overlapping_root_nested_preorder_and_independent_sides(self):
        rules = {**BASE, 'required_selectors': ['p, main, b', 'p', 'p']}
        def page(n):
            return '<p>Outside</p><main>'+''.join(f'<p>{i}<b>Nested</b></p>' for i in range(n))+'</main>'
        report = compare_html(page(12), page(2), rules)
        for side, count in (('before', 12), ('after', 2)):
            expected = ['/region'] + [p for i in range(count) for p in (f'/region/p[{i}]', f'/region/p[{i}]/b[0]')]
            observed = rows(report, side)
            self.assertEqual([r['rule_id'] for r in observed], [f'required_selectors[{i}]' for i in range(3)])
            self.assert_stages(observed[0], inventory(*expected))
            for row in observed[1:]:
                self.assert_stages(row, inventory(*(f'/region/p[{i}]' for i in range(count))))
        self.assertEqual(markdown_coverage(markdown(report)), report['rule_coverage'])

    def test_selector_drift_at_each_stage_keeps_original_same_tag_indices(self):
        page = '<main><p class="noise">Noise</p><p title="x">A</p><p>B</p><p title="y">C</p></main>'
        selectors = ['p:nth-child(even), p:first-child', '[title], p:last-child',
                     'p:not([title]), p:last-child', 'main, p', 'p:nth-child(even), p:first-child']
        rules = {**BASE, 'required_selectors': selectors, 'ignore_selectors': ['.noise'],
                 'volatile_attributes': [{'selector': 'p', 'attributes': ['title']}]}
        report = compare_html(page, page, rules)
        def ps(*indices): return inventory(*(f'/region/p[{i}]' for i in indices))
        for side in ('before', 'after'):
            observed = rows(report, side)
            for index in (0, 4): self.assert_stages(observed[index], ps(0, 1, 3), ps(1, 2))
            self.assert_stages(observed[1], ps(1, 3), ps(1, 3), ps(3))
            self.assert_stages(observed[2], ps(0, 2, 3), ps(2, 3), ps(1, 2, 3))
            self.assert_stages(observed[3], inventory('/region', *(f'/region/p[{i}]' for i in range(4))),
                               inventory('/region', *(f'/region/p[{i}]' for i in range(1, 4))))
            self.assertEqual([r['rule_id'] for r in report['rule_coverage'][side]],
                             ['ignore_selectors[0]', 'volatile_attributes[0]'] + [f'required_selectors[{i}]' for i in range(5)])
        self.assertEqual(report['changes'], [])

    def test_reordered_identities_and_keyed_collections_keep_positions_and_values(self):
        rules = {**BASE, 'required_selectors': ['article, p.keep', 'li', 'main'],
                 'ignore_selectors': ['.noise'], 'normalize_whitespace': True,
                 'sibling_identity': [{'selector': 'article', 'key_attribute': 'id'}],
                 'unordered_collections': [{'selector': 'ul', 'items': 'li', 'key_attribute': 'id'}],
                 'required_attributes': [{'selector': 'li', 'attributes': ['id']}],
                 'volatile_attributes': [{'selector': 'article', 'attributes': ['title']}]}
        def card(key, text):
            return f'<article id="{key}" title="clock"><p class="noise">Gone</p><p class="keep">{text}</p></article>'
        a,b=card('A', ' A  B '),card('B', 'Keep')
        before='<main>'+a+b+'<ul><li id="Z">Z</li><li id="A">A</li></ul></main>'
        after='<main>'+b+a.replace(' A  B ', 'A C')+'<ul><li id="A">A</li><li id="Z">Z</li></ul></main>'
        report=compare_html(before, after, rules)
        for side in ('before', 'after'):
            observed=rows(report,side)
            self.assert_stages(observed[0], inventory('/region/article[0]', '/region/article[0]/p[1]', '/region/article[1]', '/region/article[1]/p[1]'))
            self.assert_stages(observed[1], inventory('/region/ul[0]/li[0]', '/region/ul[0]/li[1]'))
            self.assert_stages(observed[2], inventory('/region'))
        change=next(c for c in report['changes'] if c['kind']=='changed')
        self.assertEqual((change['old'], change['new'], change['before_path'], change['after_path']),
                         ('A B', 'A C', '/region/article[0]/p[1]/#text[0]', '/region/article[1]/p[1]/#text[0]'))
        self.assertTrue(any(s['rule']=='unordered_collection' for s in report['suppressed']))

    def test_ambiguous_candidates_stay_in_inventory_without_suppression_claims(self):
        page='<main><article id="X"> A  B </article><article id="X"> C  D </article><article id="Y"> E  F </article></main>'
        rules={**BASE, 'required_selectors': ['article'], 'normalize_whitespace': True,
               'sibling_identity': [{'selector': 'article', 'key_attribute': 'id'}]}
        report=compare_html(page,page.replace('  ',' '),rules)
        for side in ('before','after'):
            self.assert_stages(rows(report,side)[0], inventory(*(f'/region/article[{i}]' for i in range(3))))
        self.assertTrue(any(c['kind']=='ambiguous' for c in report['changes']))
        self.assertEqual(len(report['suppressed']),1)
        self.assertEqual(report['suppressed'][0]['before_path'],'/region/article[2]/#text[0]')

    def test_omitted_empty_and_existing_text_gate_semantics(self):
        page='<main><p>Keep</p></main>'
        for rules in (BASE, {**BASE, 'required_selectors': []}):
            report=compare_html(page,page,rules)
            self.assertEqual(report['rule_coverage']['before'],[])
            self.assertEqual(report['rule_coverage']['after'],[])
        # Every selected match needs text, including later matches; attributes/comments are insufficient.
        for bad in ('<p></p>', '<p> \u00a0\u2003</p>', '<p title="value"><!--only comment--></p>', '<p><script>script only</script></p>'):
            for before,after,side in ((page,page.replace('</main>',bad+'</main>'),'after'),
                                      (page.replace('</main>',bad+'</main>'),page,'before')):
                with self.subTest(bad=bad,side=side), self.assertRaisesRegex(ContractError, side+r": required text contract failed: required_selectors\[0\] selector='p' stage=parsed empty text"):
                    compare_html(before,after,{**BASE,'required_selectors':['p']})
        # Protected ordinary text still satisfies the text contract.
        protected='<main><pre> A  B </pre><code> C  D </code></main>'
        report=compare_html(protected,protected,{**BASE,'required_selectors':['pre, code'],'normalize_whitespace':True})
        self.assert_stages(rows(report,'before')[0],inventory('/region/pre[0]','/region/code[0]'))

    def test_cli_json_markdown_writes_dry_run_and_semantic_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp); before,after,config,out=[directory/n for n in ('before.html','after.html','rules.json','out')]
            before.write_text('<main><p>One</p><p>Two</p></main>');config.write_text(json.dumps({**BASE,'required_selectors':['p']}))
            for changed in (False,True):
                after.write_text(before.read_text().replace('Two','Three') if changed else before.read_text())
                for fmt in ('json','markdown'):
                    for dry in (False,True):
                        out.write_bytes(b'previous\x00\xff')
                        result=subprocess.run(CLI+[str(before),str(after),'--rules',str(config),'--out',str(out),'--format',fmt,'--fail-on-change']+(['--dry-run'] if dry else []),cwd=CLI_CWD,text=True,capture_output=True,timeout=20)
                        self.assertEqual(result.returncode,int(changed),result.stderr)
                        text=result.stdout if dry else out.read_text()
                        coverage=json.loads(text)['rule_coverage'] if fmt=='json' else markdown_coverage(text)
                        self.assertEqual(coverage['before'],coverage['after'])
                        self.assert_stages(coverage['before'][0],inventory('/region/p[0]','/region/p[1]'))
                        if dry: self.assertEqual(out.read_bytes(),b'previous\x00\xff')
                        else: self.assertEqual(result.stdout,'')

    def test_cli_missing_empty_and_stage_failures_never_write_reports(self):
        good='<main><p title="x">Keep</p><p>Other</p></main>'
        cases=[('<main><h1>Keep</h1></main>',good,{'required_selectors':['p']}),
               (good.replace('Keep',' \u00a0\u2003'),good,{'required_selectors':['p']}),
               ('<main><p class="noise">Gone</p><h1>Keep</h1></main>',
                '<main><p class="noise">Gone</p><p>Keep</p></main>',
                {'required_selectors':['p'],'ignore_selectors':['.noise']}),
               ('<main><p title="x">Keep</p></main>',good.replace('<p>Other', '<p title="y">Other'),
                {'required_selectors':['p[title]'],'volatile_attributes':[{'selector':'p:first-child','attributes':['title']}]}),
               ('<main><p class="noise">Gone</p><h1> </h1></main>',
                '<main><p class="noise">Gone</p><h1>Keep</h1></main>',
                {'required_selectors':['main'],'ignore_selectors':['.noise']})]
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp);before,after,config,out=[directory/n for n in ('before.html','after.html','rules.json','out')]
            for bad,good,update in cases:
                config.write_text(json.dumps({**BASE,**update}))
                for first,second,side in ((bad,good,'before'),(good,bad,'after')):
                    before.write_text(first);after.write_text(second)
                    for flags in ([],['--format','markdown','--fail-on-change'],['--dry-run','--fail-on-change']):
                        for exists in (False,True):
                            out.unlink(missing_ok=True)
                            if exists: out.write_bytes(b'previous\x00\xff')
                            result=subprocess.run(CLI+[str(before),str(after),'--rules',str(config),'--out',str(out),*flags],cwd=CLI_CWD,text=True,capture_output=True,timeout=20)
                            self.assertEqual(result.returncode,2,result.stderr)
                            self.assertEqual(result.stdout,'')
                            self.assertIn(side+': required',result.stderr)
                            self.assertEqual(out.read_bytes() if exists else out.exists(),b'previous\x00\xff' if exists else False)
                            self.assertFalse(list(directory.glob('.stable-diff-*')))
