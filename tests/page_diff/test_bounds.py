"""Inclusive selector bounds reject excessive suppression on local fixtures."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from qawrench.page_diff import ContractError, compare_files, compare_html, load_rules, validate_rules

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'examples' / 'page_diff' / 'bounds'
BASE = {'version': 1, 'required_region': 'main'}
ENTRIES = [('ignore_selectors', {'selector': '.noise'}),
           ('volatile_attributes', {'selector': '.noise', 'attributes': ['data-render']})]


def page(body):
    return '<main><h1>Catalog</h1>' + body + '</main>'


class MatchBoundTests(unittest.TestCase):
    def test_broadened_fixture_rules_reject_hidden_product_price(self):
        for name, field, selector in [('ignore', 'ignore_selectors', '.clock, .product'),
                                      ('volatile', 'volatile_attributes', '.render, .product')]:
            with self.subTest(rule=name):
                # Without the upper bound, these rules hide the actual $10->$12 edit.
                rules = json.loads((FIXTURES / f'broadened-{name}-rules.json').read_text())
                unbounded = deepcopy(rules)
                for entries in (unbounded['ignore_selectors'], unbounded['volatile_attributes']):
                    for entry in entries:
                        entry.pop('max_matches')
                hidden = compare_files(FIXTURES / 'before.html', FIXTURES / 'after.html', unbounded)
                self.assertEqual((hidden['status'], hidden['changes']), ('unchanged', []))
                self.assertIn('$12', json.dumps(hidden['suppressed']))
                with self.assertRaises(ContractError) as raised:
                    compare_files(FIXTURES / 'before.html', FIXTURES / 'after.html', rules)
                for detail in ('before:', 'normalization coverage failed', f'{field}[0]',
                               selector, 'expected at most 1', 'got 2', 'catalog-team'):
                    self.assertIn(detail, str(raised.exception))

    def test_exact_bounds_keep_product_edit_and_expose_coverage_without_mutation(self):
        rules = load_rules(FIXTURES / 'rules.json')
        original = deepcopy(rules)
        report = compare_files(FIXTURES / 'before.html', FIXTURES / 'after.html', rules)
        self.assertEqual(report['status'], 'changed')
        self.assertEqual([(c['old'], c['new']) for c in report['changes']], [('$10', '$12')])
        self.assertEqual(len(report['suppressed']), 2)
        for side in ('before', 'after'):
            self.assertEqual([(r['matched_elements'], r['min_matches'], r['max_matches'])
                              for r in report['rule_coverage'][side]], [(1, 1, 1), (1, 1, 1)])
        self.assertEqual(report['rules'], rules)
        self.assertEqual(rules, original)
        self.assertEqual(validate_rules(report['rules']), rules)

    def test_excess_matches_fail_on_either_side_even_for_identical_snapshots(self):
        one = page('<p class="noise">one</p>')
        two = page('<p class="noise">one</p><p class="noise">two</p>')
        for field, entry in ENTRIES:
            for before, after, side in [(one, two, 'after'), (two, one, 'before'), (two, two, 'before')]:
                with self.subTest(field=field, side=side):
                    with self.assertRaisesRegex(ContractError, side + r':.*expected at most 1.*got 2'):
                        compare_html(before, after, {**BASE, field: [{**entry, 'max_matches': 1}]})

    def test_inclusive_range_and_explicit_zero_maximum(self):
        for field, entry in ENTRIES:
            for minimum, maximum in [(0, 0), (0, 2), (1, 2), (2, 2)]:
                rules = {**BASE, field: [{**entry, 'min_matches': minimum, 'max_matches': maximum}]}
                for count in range(4):
                    html = page('<p class="noise">noise</p>' * count)
                    with self.subTest(field=field, minimum=minimum, maximum=maximum, count=count):
                        if minimum <= count <= maximum:
                            report = compare_html(html, html, rules)
                            row = report['rule_coverage']['before'][0]
                            self.assertEqual((row['matched_elements'], row['max_matches']), (count, maximum))
                        else:
                            with self.assertRaisesRegex(ContractError, 'normalization coverage failed'):
                                compare_html(html, html, rules)

    def test_ignore_bound_counts_nested_matches_before_removal_per_rule(self):
        html = page('<aside class="noise"><p class="noise">nested</p></aside>')
        rules = {**BASE, 'ignore_selectors': [{'selector': '.noise', 'max_matches': 1}]}
        with self.assertRaisesRegex(ContractError, r'ignore_selectors\[0\].*at most 1.*got 2'):
            compare_html(html, html, rules)
        rules['ignore_selectors'][0]['max_matches'] = 2
        rules['ignore_selectors'].append({'selector': '.noise', 'max_matches': 2})
        report = compare_html(html, html, rules)
        self.assertEqual([r['matched_elements'] for r in report['rule_coverage']['before']], [2, 2])
        self.assertEqual(len(report['normalizations']['before']), 1)

    def test_volatile_bound_counts_post_ignore_elements_even_without_attributes(self):
        html = page('<aside><p class="noise" data-render="a">ignored</p></aside>'
                    '<p class="noise">kept</p>')
        rules = {**BASE, 'ignore_selectors': ['aside'], 'volatile_attributes': [
            {'selector': '.noise', 'attributes': ['data-render'], 'max_matches': 1},
            {'selector': '.noise', 'attributes': ['title'], 'max_matches': 1}]}
        report = compare_html(html, html, rules)
        for row in report['rule_coverage']['before'][1:]:
            self.assertEqual(row['matched_elements'], 1)
            self.assertEqual(list(row['attributes_present'].values()), [0])
        del rules['ignore_selectors']
        with self.assertRaisesRegex(ContractError, r'volatile_attributes\[0\].*at most 1.*got 2'):
            compare_html(html, html, rules)

    def test_volatile_bound_includes_region_root(self):
        html = page('<p>keep</p>')
        rules = {**BASE, 'volatile_attributes': [{'selector': 'main, p', 'attributes': ['id'], 'max_matches': 1}]}
        with self.assertRaisesRegex(ContractError, r'at most 1.*got 2'):
            compare_html(html, html, rules)

    def test_omitted_maximum_stays_unbounded_and_absent_from_coverage(self):
        html = page('<p class="noise">noise</p>' * 3)
        for field, entry in [*ENTRIES, ('ignore_selectors', '.noise')]:
            with self.subTest(field=field, entry=entry):
                report = compare_html(html, html, {**BASE, field: [entry]})
                row = report['rule_coverage']['before'][0]
                self.assertEqual(row['matched_elements'], 3)
                self.assertNotIn('max_matches', row)

    def test_maximum_validation_and_effective_minimum(self):
        for field, entry in ENTRIES:
            for maximum in (True, False, -1, 1.5, '1', None, [], {}):
                with self.subTest(field=field, maximum=maximum):
                    with self.assertRaisesRegex(ContractError, field + r'\[0\].max_matches must be a nonnegative integer'):
                        validate_rules({**BASE, field: [{**entry, 'max_matches': maximum}]})
            for minimum, maximum in [(1, 0), (2, 1)]:
                bounds = {'max_matches': maximum}
                if minimum != 1:  # Also test the implicit default minimum of 1.
                    bounds['min_matches'] = minimum
                with self.subTest(field=field, bounds=bounds):
                    with self.assertRaisesRegex(ContractError, r'max_matches must be >= min_matches'):
                        validate_rules({**BASE, field: [{**entry, **bounds}]})

    def test_maximum_is_not_accepted_on_other_rule_types(self):
        for field, entry in [('unordered_collections', {'selector': 'section', 'items': 'p', 'key_attribute': 'id'}),
                             ('sibling_identity', {'selector': 'p', 'key_attribute': 'id'})]:
            with self.subTest(field=field), self.assertRaises(ContractError):
                validate_rules({**BASE, field: [{**entry, 'max_matches': 1}]})


class MatchBoundCliTests(unittest.TestCase):
    def cli(self, rules, *extra):
        return subprocess.run([sys.executable, '-m', 'qawrench.page_diff', str(FIXTURES / 'before.html'),
                               str(FIXTURES / 'after.html'), '--rules', str(rules), *map(str, extra)],
                              cwd=ROOT, text=True, capture_output=True, timeout=20)

    def test_broadened_rules_fail_without_creating_or_replacing_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'report.json'
            for name, field in [('ignore', 'ignore_selectors'), ('volatile', 'volatile_attributes')]:
                for extra in [(), ('--fail-on-change',), ('--dry-run',), ('--format', 'markdown')]:
                    for existing in (False, True):
                        if existing:
                            output.write_text('previous report')
                        else:
                            output.unlink(missing_ok=True)
                        with self.subTest(name=name, extra=extra, existing=existing):
                            result = self.cli(FIXTURES / f'broadened-{name}-rules.json', '--out', output, *extra)
                            self.assertEqual(result.returncode, 2, result.stderr)
                            for detail in ('before:', f'{field}[0]', 'expected at most 1', 'got 2', 'catalog-team'):
                                self.assertIn(detail, result.stderr)
                            self.assertEqual(result.stdout, '')
                            if existing:
                                self.assertEqual(output.read_text(), 'previous report')
                            else:
                                self.assertFalse(output.exists())

    def test_json_and_markdown_expose_bound_and_retained_price_edit(self):
        result = self.cli(FIXTURES / 'rules.json', '--fail-on-change')
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual([(c['old'], c['new']) for c in report['changes']], [('$10', '$12')])
        self.assertEqual([r['max_matches'] for r in report['rule_coverage']['after']], [1, 1])
        result = self.cli(FIXTURES / 'rules.json', '--format', 'markdown')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"max_matches": 1', result.stdout)
        self.assertIn('$10', result.stdout)
        self.assertIn('$12', result.stdout)
