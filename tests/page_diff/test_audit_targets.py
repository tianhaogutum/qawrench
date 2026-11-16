"""Every volatile observation retains its element, even without a suppression."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from qawrench.page_diff import compare_files, compare_html, load_rules
from qawrench.page_diff.cli import markdown

EXAMPLES = Path(__file__).resolve().parents[2] / 'examples' / 'page_diff'
AUDIT = EXAMPLES / 'audit-targets'
BASE = {'version': 1, 'required_region': 'main'}


def volatile_events(report, side):
    return [e for e in report['normalizations'][side] if e['rule'] == 'volatile_attributes']


def markdown_audit(text):
    return json.loads(text.split('## Full normalization audit\n', 1)[1].split('```json\n', 1)[1].split('\n```', 1)[0])


class AuditTargetTests(unittest.TestCase):
    def test_unchanged_missing_empty_and_present_attributes_keep_targets(self):
        report = compare_files(AUDIT / 'snapshot.html', AUDIT / 'snapshot.html', load_rules(AUDIT / 'rules.json'))
        self.assertEqual((report['status'], report['changes'], report['suppressed']), ('unchanged', [], []))
        for side in ('before', 'after'):
            events = volatile_events(report, side)
            self.assertEqual(len(events), 3)
            for index, (event, old) in enumerate(zip(events, (None, '', '<`>')), 1):
                with self.subTest(side=side, index=index):
                    self.assertEqual(event['original_target_path'], f'/region/article[{index}]')
                    self.assertEqual(event['original_path'], f'/region/article[{index}]/@data-render')
                    self.assertEqual((event['old'], event['new']), (old, None))
                    self.assertEqual([r['rule_id'] for r in event['rules']],
                                     [f'volatile_attributes[{i}]' for i in (0, 1)] +
                                     (['volatile_attributes[2]'] if old is not None else []))
            rows = {r['rule_id']: r for r in report['rule_coverage'][side]}
            self.assertEqual([rows[f'volatile_attributes[{i}]']['matched_elements'] for i in range(3)], [3, 3, 2])
            self.assertEqual(rows['volatile_attributes[0]']['attributes_present'], {'data-render': 2})
            self.assertTrue(all('original_target_path' not in e for e in report['normalizations'][side] if e['rule'] != 'volatile_attributes'))

    def test_reordered_targets_keep_original_indices_and_existing_suppression_locations(self):
        directory = EXAMPLES / 'suppression-targets'
        report = compare_files(directory / 'before.html', directory / 'after.html', load_rules(directory / 'rules.json'))
        for side, indices in (('before', [1, 2]), ('after', [1, 2])):
            events = volatile_events(report, side)
            self.assertEqual([e['original_target_path'] for e in events], [f'/region/article[{i}]' for i in indices])
            for event in events:
                self.assertEqual(event['original_path'], event['original_target_path'] + '/@data-render')
        for finding in report['suppressed']:
            if finding['rule'] != 'volatile_attributes':
                continue
            for side in ('before', 'after'):
                event = next(e for e in volatile_events(report, side) if e['path'] == finding['path'])
                self.assertEqual(event['original_target_path'], finding[side + '_target_path'])
                self.assertEqual(finding[side + '_path'], event['original_path'] if event['old'] is not None else None)
                self.assertEqual(finding[side + '_rules'], event['rules'])
        self.assertEqual(len(report['suppressed']), 3)
        self.assertEqual(sorted(c['kind'] for c in report['changes']), ['changed', 'moved', 'moved'])

    def test_unobserved_side_has_no_entry_despite_existing_unselected_element(self):
        rules = {**BASE, 'volatile_attributes': [{'selector': '.render', 'attributes': ['title'], 'min_matches': 0}]}
        plain = '<main><p title="semantic">Keep</p></main>'
        selected = '<main><p class="render" title="noise">Keep</p></main>'
        for a, b, absent in ((plain, selected, 'before'), (selected, plain, 'after')):
            with self.subTest(absent=absent):
                report = compare_html(a, b, rules)
                present = 'after' if absent == 'before' else 'before'
                self.assertEqual(volatile_events(report, absent), [])
                self.assertEqual(volatile_events(report, present)[0]['original_target_path'], '/region/p[0]')
                self.assertIsNone(report['suppressed'][0][absent + '_target_path'])
                self.assertEqual(report['suppressed'][0][absent + '_rules'], [])
                self.assertTrue(any(c['path'].endswith('/attributes/title') for c in report['changes']))

    def test_ambiguous_observations_retain_independent_targets_without_claims(self):
        rules = {**BASE, 'sibling_identity': [{'selector': 'article', 'key_attribute': 'id'}],
                 'volatile_attributes': [{'selector': '.render', 'attributes': ['title'], 'min_matches': 0}]}
        for key in (' id="A"', ''):
            duplicate = f'<main><article{key} class="render">One</article><article{key} class="render" title="">Two</article></main>'
            unique = f'<main><article{key} title="unselected">One</article></main>'
            for a, b, selected in ((duplicate, unique, 'before'), (unique, duplicate, 'after')):
                with self.subTest(key=key, selected=selected):
                    report = compare_html(a, b, rules)
                    self.assertEqual(report['suppressed'], [])
                    self.assertTrue(any(c['kind'] == 'ambiguous' for c in report['changes']))
                    events = volatile_events(report, selected)
                    self.assertEqual([e['original_target_path'] for e in events], ['/region/article[0]', '/region/article[1]'])
                    self.assertEqual([e['old'] for e in events], [None, ''])
                    self.assertTrue(all(e['ambiguous_identity'] for e in events))
                    self.assertEqual(volatile_events(report, 'after' if selected == 'before' else 'before'), [])

    def test_root_keyed_collection_and_multiple_attributes_retain_element_paths(self):
        rules = {**BASE, 'volatile_attributes': [{'selector': 'main, article', 'attributes': ['title', 'data-render']}],
                 'unordered_collections': [{'selector': 'section', 'items': 'article', 'key_attribute': 'id'}]}
        a = '<main title=""><section><article id="a/b~c">A</article><article id="B" title="b">B</article></section></main>'
        b = '<main title=""><section><article id="B" title="b">B</article><article id="a/b~c">A</article></section></main>'
        report = compare_html(a, b, rules)
        self.assertEqual(report['changes'], [])
        self.assertEqual(len(report['suppressed']), 1)  # Only collection order differs.
        for side in ('before', 'after'):
            events = volatile_events(report, side)
            self.assertEqual([e['original_target_path'] for e in events],
                             ['/region'] * 2 + ['/region/section[0]/article[0]'] * 2 + ['/region/section[0]/article[1]'] * 2)
            self.assertTrue(any('a~1b~0c' in e['path'] for e in events))
            for event in events:
                self.assertEqual(event['original_path'].rsplit('/@', 1)[0], event['original_target_path'])

    def test_markdown_full_audit_exposes_escaped_targets_without_mutating_report(self):
        report = compare_files(AUDIT / 'snapshot.html', AUDIT / 'snapshot.html', load_rules(AUDIT / 'rules.json'))
        original = deepcopy(report)
        text = markdown(report)
        self.assertEqual(markdown_audit(text), report['normalizations'])
        self.assertEqual(text.count('"original_target_path"'), 6)
        self.assertIn('"original_target_path": "/region/article[1]"', text)
        self.assertIn('\\u003c\\u0060\\u003e', text)
        self.assertNotIn('<`>', text)
        self.assertEqual(report, original)

    def test_cli_unchanged_audit_and_nested_coverage_errors(self):
        command = [sys.executable, '-m', 'qawrench.page_diff', str(AUDIT / 'snapshot.html'), str(AUDIT / 'snapshot.html'),
                   '--rules', str(AUDIT / 'rules.json'), '--fail-on-change']
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['suppressed'], [])
        self.assertEqual(volatile_events(report, 'before')[0]['original_target_path'], '/region/article[1]')
        result = subprocess.run(command + ['--format', 'markdown'], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(markdown_audit(result.stdout), report['normalizations'])
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            rules = load_rules(AUDIT / 'rules.json')
            rules['ignore_selectors'] = [{'selector': '.noise, .inner', 'max_matches': 1}]
            path, output = td / 'rules.json', td / 'report.json'
            path.write_text(json.dumps(rules))
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
