"""Essential attributes are independent input contracts, never noise rules."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from qawrench.page_diff import ContractError, compare_html, validate_rules
from qawrench.page_diff.cli import markdown

ROOT = Path(__file__).resolve().parents[2]
BASE = {'version': 1, 'required_region': 'main'}
RULE = {'selector': 'a.product', 'attributes': ['href', 'data-sku']}
RULES = {**BASE, 'required_attributes': [RULE]}
GOOD = '<main><a class="product" href="/one" data-sku="A">Product</a></main>'


def inventory(*paths):
    return {'matched_elements': len(paths), 'matched_nodes': [{'original_path': p} for p in paths]}


def rows(report, side):
    return [r for r in report['rule_coverage'][side] if r['rule_id'].startswith('required_attributes[')]


class RequiredAttributesTests(unittest.TestCase):
    def failure(self, before, after, rules, *details):
        with self.assertRaises(ContractError) as error:
            compare_html(before, after, rules)
        message = str(error.exception)
        for detail in ('required_attributes[0]', *details):
            self.assertIn(detail, message)
        self.assertNotIn('/private-value', message)
        return message

    def test_valid_snapshot_has_exact_per_side_coverage_and_markdown(self):
        original = deepcopy(RULES)
        report = compare_html(GOOD, GOOD, RULES)
        located = inventory('/region/a[0]')
        expected = {'rule_id': 'required_attributes[0]', **RULE, **located,
                    'after_ignores': located, 'after_volatile': located}
        for side in ('before', 'after'):
            self.assertEqual(rows(report, side), [expected])
        self.assertEqual((report['status'], report['changes'], report['suppressed']), ('unchanged', [], []))
        self.assertEqual(report['normalizations'], {'before': [], 'after': []})
        coverage = markdown(report).split('## Normalization rule coverage\n', 1)[1].split('```json\n', 1)[1].split('\n```', 1)[0]
        self.assertEqual(json.loads(coverage), report['rule_coverage'])
        self.assertEqual(RULES, original)

    def test_missing_empty_and_unicode_blank_each_fail_on_either_side(self):
        for attribute in ('href', 'data-sku'):
            value = '/one' if attribute == 'href' else 'A'
            for replacement, reason in (('', 'missing'), (f'{attribute}=""', 'blank'),
                                        (f'{attribute}', 'blank'),
                                        (f'{attribute}=" \t\n\u00a0\u1680\u2003\u2028\u202f\u3000"', 'blank'),
                                        (f'{attribute}="&#160;&#x2003;"', 'blank')):
                bad = GOOD.replace(f'{attribute}="{value}"', replacement)
                for before, after, side in ((bad, GOOD, 'before'), (GOOD, bad, 'after')):
                    with self.subTest(attribute=attribute, replacement=replacement, side=side):
                        self.failure(before, after, RULES, side + ':', attribute, reason,
                                     'stage=parsed', "original_path='/region/a[0]'")

    def test_every_match_is_checked_in_dom_order_with_original_paths(self):
        prefix = '<a class="noise">Noise</a><!-- keep original element counts -->'
        good = '<a class="product" href="/private-value" data-sku="A">Product</a>'
        bad = '<a class="product" data-sku="B">Product</a>'
        html = '<main>' + prefix + good + bad + '</main>'
        rules = {**RULES, 'ignore_selectors': ['.noise']}
        self.failure(html, html, rules, 'href', 'missing', "original_path='/region/a[2]'")
        # An invalid original match cannot be hidden by an ignore or volatile rule.
        for update in ({'ignore_selectors': ['.product']},
                       {'volatile_attributes': [{'selector': '.product', 'attributes': ['href']}]}):
            self.failure(html, html, {**RULES, **update}, 'stage=parsed', "original_path='/region/a[2]'")

    def test_zero_matches_is_a_gate_even_if_outside_region_match_exists(self):
        html = '<a class="product" href="/private-value" data-sku="X">Outside</a><main>Catalog</main>'
        self.failure(html, GOOD, RULES, 'before:', 'href', 'data-sku', 'got 0',
                     'original_path=null', "original_scope_path='/region'")
        self.failure(GOOD, html, RULES, 'after:', 'got 0')

    def test_region_root_nested_matches_preorder_and_independent_rule_sides(self):
        rules = {**BASE, 'required_attributes': [
            {'selector': 'a, main', 'attributes': ['data-sku']}, RULE, RULE]}
        a = '<a class="product" href="/one" data-sku="A">A</a>'
        html = '<main data-sku="root"><section data-sku="ignored">' + a * 12 + '</section></main>'
        after = html.replace(a * 12, a)
        report = compare_html(html, after, rules)
        for side, count in (('before', 12), ('after', 1)):
            paths = [f'/region/section[0]/a[{i}]' for i in range(count)]
            for index, expected in ((0, inventory('/region', *paths)), (1, inventory(*paths)), (2, inventory(*paths))):
                row = rows(report, side)[index]
                self.assertEqual({k: row[k] for k in expected}, expected)
                self.assertEqual(row['after_ignores'], expected)
                self.assertEqual(row['after_volatile'], expected)
        self.failure('<main>Catalog</main>', html, rules, 'data-sku', "original_path='/region'")

    def test_ignored_required_descendant_fails_even_when_another_match_survives(self):
        link = '<a class="product" href="/one" data-sku="A">Product</a>'
        html = '<main>' + link + '<aside>' + link + '</aside></main>'
        for selector in ('aside', 'aside a'):
            self.failure(html, html, {**RULES, 'ignore_selectors': [selector]}, 'stage=after_ignores',
                         'href', 'removed element', "original_path='/region/aside[0]/a[0]'")

    def test_required_volatile_attribute_fails_after_removal_with_original_path(self):
        html = GOOD.replace('<a class=', '<a class="noise">Noise</a><a class=')
        rules = {**RULES, 'ignore_selectors': ['.noise'], 'volatile_attributes': [
            {'selector': '.product', 'attributes': ['href']}]}
        self.failure(html, html, rules, 'stage=after_volatile', 'href', 'missing', "original_path='/region/a[1]'")

    def test_selector_membership_loss_cannot_drop_an_earlier_obligation(self):
        html = '<main><a title="selected" href="/one">A</a><a title="selected" href="/two">B</a></main>'
        rules = {**BASE, 'required_attributes': [{'selector': '[title]', 'attributes': ['href']}],
                 'volatile_attributes': [{'selector': 'a:first-child', 'attributes': ['title', 'href']}]}
        # The first link ceases to match, while the second keeps match count nonzero.
        self.failure(html, html, rules, 'stage=after_volatile', 'href', 'missing', "original_path='/region/a[0]'")

    def test_reselection_covers_new_matches_and_keeps_earlier_obligations(self):
        html = '<main><span>Noise</span><a href="/one">A</a></main>'
        rules = {**BASE, 'required_attributes': [{'selector': 'span:first-child, a:first-child', 'attributes': ['title']}],
                 'ignore_selectors': ['span']}
        # Removing a selected node fails before considering its replacement.
        self.failure(html.replace('<span>', '<span title="valid">'), html, rules,
                     'stage=after_ignores', 'removed element')
        # Structural selectors can gain candidates without removing an earlier match.
        html = '<main><a href="/one">A</a><span>Noise</span><a>B</a></main>'
        rules = {**BASE, 'required_attributes': [{'selector': 'a:nth-child(odd)', 'attributes': ['href']}],
                 'ignore_selectors': ['span']}
        self.failure(html, html, rules, 'stage=parsed', "original_path='/region/a[1]'")
        rules['required_attributes'][0]['selector'] = 'a:nth-child(even), a:first-child'
        self.failure(html, html, rules, 'stage=after_ignores', 'href', "original_path='/region/a[1]'")
        rules['volatile_attributes'] = [{'selector': 'a', 'attributes': ['href']}]
        html = html.replace('<a>B', '<a href="/two">B')
        self.failure(html, html, rules, 'stage=after_volatile', 'href', "original_path='/region/a[0]'")

    def test_zero_after_volatile_and_selector_changes_have_stage_coverage(self):
        rules = {**RULES, 'required_attributes': [{'selector': '[title]', 'attributes': ['href']}],
                 'volatile_attributes': [{'selector': 'a', 'attributes': ['title']}]}
        html = GOOD.replace('href=', 'title="noise" href=')
        self.failure(html, html, rules, 'stage=after_volatile', 'got 0', 'href')
        rules = {**BASE, 'required_attributes': [{'selector': 'a:nth-child(even), a:first-child', 'attributes': ['href']}],
                 'ignore_selectors': ['span']}
        html = '<main><a href="/one">A</a><span>Noise</span><a href="/two">B</a></main>'
        row = rows(compare_html(html, html, rules), 'before')[0]
        self.assertEqual(row['matched_nodes'], inventory('/region/a[0]')['matched_nodes'])
        self.assertEqual(row['after_ignores'], inventory('/region/a[0]', '/region/a[1]'))
        self.assertEqual(row['after_volatile'], row['after_ignores'])

    def test_exact_values_are_not_normalized_and_no_url_validation_is_implied(self):
        rules = {**RULES, 'normalize_whitespace': True}
        for value in ('  /two\u00a0 ', '\u200b', 'not a URL', 'javascript:inert()', 'e\u0301'):
            before = GOOD.replace('/one', '/private-value')
            after = GOOD.replace('/one', value)
            report = compare_html(before, after, rules)
            self.assertEqual(len(report['changes']), 1)
            change = report['changes'][0]
            self.assertEqual((change['kind'], change['old'], change['new']), ('changed', '/private-value', value))
            self.assertEqual(change['before_path'], '/region/a[0]/@href')
            self.assertEqual(change['after_path'], '/region/a[0]/@href')

    def test_strict_additive_schema_and_omission(self):
        for value in (None, True, {}, 'a'):
            with self.subTest(value=value), self.assertRaises(ContractError):
                validate_rules({**BASE, 'required_attributes': value})
        entries = [None, True, 'a', {}, {'selector': 'a'}, {'attributes': ['href']},
                   {**RULE, 'owner': 'team'}, {**RULE, 'min_matches': 0},
                   {**RULE, 'selector': '['}, {**RULE, 'selector': ''}, {**RULE, 'selector': False}]
        entries += [{**RULE, 'attributes': value} for value in
                    (None, True, {}, 'href', [], ['href', 'href'], ['HREF'], [' href'], ['a b'], [''],
                     [None], [True], [0], [[]], [{}], ['href', []])]
        for entry in entries:
            with self.subTest(entry=entry), self.assertRaises(ContractError):
                validate_rules({**BASE, 'required_attributes': [entry]})
        self.assertNotIn('required_attributes', validate_rules(BASE))
        empty = {**BASE, 'required_attributes': []}
        self.assertEqual(validate_rules(empty)['required_attributes'], [])
        self.assertEqual(rows(compare_html(GOOD, GOOD, empty), 'before'), [])

    def test_cli_negative_preserves_output_under_exit_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            before, after, rules, out = [directory / n for n in ('before.html', 'after.html', 'rules.json', 'out.json')]
            before.write_text(GOOD)
            rules.write_text(json.dumps(RULES))
            for bad in (GOOD.replace('href="/one"', ''), GOOD.replace('/one', '\u00a0\u2003')):
                after.write_text(bad)
                for flags in ((), ('--fail-on-change', '--format', 'markdown'), ('--dry-run', '--fail-on-change')):
                    for existing in (False, True):
                        out.unlink(missing_ok=True)
                        if existing:
                            out.write_bytes(b'previous report\n')
                        result = subprocess.run([sys.executable, '-m', 'qawrench.page_diff', str(before), str(after),
                                                 '--rules', str(rules), '--out', str(out), *flags],
                                                cwd=ROOT, text=True, capture_output=True, timeout=20)
                        self.assertEqual(result.returncode, 2, result.stderr)
                        self.assertEqual(result.stdout, '')
                        for detail in ('after:', 'required_attributes[0]', 'href', '/region/a[0]'):
                            self.assertIn(detail, result.stderr)
                        self.assertEqual(out.read_bytes(), b'previous report\n') if existing else self.assertFalse(out.exists())
