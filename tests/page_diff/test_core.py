"""Behavioral tests using synthetic offline HTML, including deliberate failures."""
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from qawrench.page_diff import ContractError, compare_html, compare_files, load_rules, validate_rules

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "page_diff"
BASE = {"version": 1, "required_region": "main"}


def page(body):
    return f"<main><h1>Catalog</h1>{body}</main>"


def item(key="a", text="$10", attrs=""):
    return f'<article data-id="{key}" {attrs}><p>{text}</p></article>'


def collection(*items):
    return page('<section class="items">' + ''.join(items) + '</section>')


def keyed_rules(**updates):
    return {**BASE, "unordered_collections": [{"selector": ".items", "items": "article", "key_attribute": "data-id"}], **updates}


class ComparisonTests(unittest.TestCase):
    def test_identical(self):
        result = compare_html(page("<p>Hello</p>"), page("<p>Hello</p>"), BASE)
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["suppressed"], [])

    def test_default_order_matters(self):
        a, b = item("a"), item("b", "$20")
        result = compare_html(collection(a, b), collection(b, a), BASE)
        self.assertEqual(result["status"], "changed")
        self.assertTrue(result["changes"])

    def test_explicit_reorder_audit(self):
        a, b = item("a"), item("b", "$20")
        result = compare_html(collection(a, b), collection(b, a), keyed_rules())
        self.assertEqual(result["changes"], [])
        descriptors = [{"rule_id": "unordered_collections[0]", "selector": ".items"}]
        self.assertEqual(result["suppressed"], [{"rule": "unordered_collection", "path": "/region/section[0]", "old": ["a", "b"], "new": ["b", "a"], "normalized": ["a", "b"], "before_path": "/region/section[0]", "after_path": "/region/section[0]", "before_rules": descriptors, "after_rules": descriptors}])

    def test_reorder_only_applies_in_declared_container(self):
        a = collection(item()).replace('</main>', '<ol><li>x</li><li>y</li></ol></main>')
        b = a.replace('<li>x</li><li>y</li>', '<li>y</li><li>x</li>')
        self.assertTrue(compare_html(a, b, keyed_rules())["changes"])

    def test_price_change_survives_reorder(self):
        result = compare_html(collection(item("a"), item("b")), collection(item("b"), item("a", "$12")), keyed_rules())
        self.assertEqual(len(result["changes"]), 1)
        change = result["changes"][0]
        self.assertEqual((change["old"], change["new"]), ("$10", "$12"))
        self.assertIn('item[data-id="a"]', change["path"])

    def test_addition_removal_and_last_removal(self):
        for before, after, kind, key in [
            (collection(item()), collection(item(), item("b")), "added", "b"),
            (collection(item(), item("b")), collection(item()), "removed", "b"),
            (collection(item()), collection(), "removed", "a"),
            (collection(), collection(item()), "added", "a"),
        ]:
            with self.subTest(kind=kind, key=key):
                changes = compare_html(before, after, keyed_rules())["changes"]
                self.assertEqual(len(changes), 1)
                self.assertEqual(changes[0]["kind"], kind)
                self.assertIn(f'item[data-id="{key}"]', changes[0]["path"])
                self.assertIn('$10', json.dumps(changes[0]))

    def test_attributes_and_tag_names_preserved(self):
        for old, new in [('<a href="/one">x</a>', '<a href="/two">x</a>'), ('<p>x</p>', '<h2>x</h2>'), ('<p class="a b">x</p>', '<p class="b a">x</p>')]:
            self.assertTrue(compare_html(page(old), page(new), BASE)["changes"])

    def test_attribute_order_and_entity_spelling_are_parser_equivalent(self):
        self.assertEqual(compare_html(page('<p a="1" b="2">&amp;</p>'), page('<p b="2" a="1">&#38;</p>'), BASE)["changes"], [])

    def test_comments_preserved(self):
        changes = compare_html(page('<!--old--><p>x</p>'), page('<!--new--><p>x</p>'), BASE)["changes"]
        self.assertEqual((changes[0]["old"], changes[0]["new"]), ("old", "new"))

    def test_nested_ignored_elements(self):
        rules = {**BASE, "ignore_selectors": [".noise", ".inner"]}
        a = page('<aside class="noise">old <em class="inner">nested</em></aside><p>keep</p>')
        b = page('<aside class="noise">new <em class="inner">changed</em></aside><p>keep</p>')
        result = compare_html(a, b, rules)
        self.assertFalse(result["changes"])
        self.assertEqual(len(result["suppressed"]), 1)
        self.assertIn('nested', result["suppressed"][0]["old"])
        self.assertIn('changed', result["suppressed"][0]["new"])

    def test_ignored_addition_is_explained(self):
        result = compare_html(page('<p>keep</p>'), page('<p>keep</p><aside>new</aside>'), {**BASE, "ignore_selectors": [{"selector": "aside", "min_matches": 0}]})
        self.assertFalse(result["changes"])
        self.assertEqual(result["suppressed"][0]["old"], None)
        self.assertIn('new', result["suppressed"][0]["new"])

    def test_volatile_attributes_and_overlap(self):
        rules = keyed_rules(volatile_attributes=[{"selector": "article", "attributes": ["data-render"]}, {"selector": "[data-id]", "attributes": ["data-render"]}])
        result = compare_html(collection(item(attrs='data-render="1"')), collection(item(attrs='data-render="2"')), rules)
        self.assertFalse(result["changes"])
        self.assertEqual(len(result["suppressed"]), 1)
        self.assertEqual(result["suppressed"][0]["rule"], "volatile_attributes")
        self.assertEqual((result["suppressed"][0]["old"], result["suppressed"][0]["new"]), ("1", "2"))

    def test_volatile_attribute_addition(self):
        result = compare_html(collection(item()), collection(item(attrs='data-render="new"')), keyed_rules(volatile_attributes=[{"selector": "article", "attributes": ["data-render"]}]))
        self.assertFalse(result["changes"])
        self.assertEqual(result["suppressed"][0]["new"], "new")

    def test_whitespace_opt_in_and_audit(self):
        a, b = page('<p>Price $10</p>'), page('<p>  Price\n $10  </p>')
        self.assertTrue(compare_html(a, b, BASE)["changes"])
        result = compare_html(a, b, {**BASE, "normalize_whitespace": True})
        self.assertFalse(result["changes"])
        event = result["suppressed"][0]
        self.assertEqual((event["old"], event["new"], event["normalized"]), ("Price $10", "  Price\n $10  ", "Price $10"))

    def test_default_preserves_whitespace_only_nodes(self):
        a = page('<p>A</p> \n  <p>B</p>')
        b = page('<p>A</p>\n<p>B</p>')
        result = compare_html(a, b, BASE)
        self.assertEqual(len(result['changes']), 1)
        self.assertEqual((result['changes'][0]['old'], result['changes'][0]['new']), (' \n  ', '\n'))
        normalized = compare_html(a, b, {**BASE, 'normalize_whitespace': True})
        self.assertFalse(normalized['changes'])
        self.assertEqual(normalized['suppressed'][0]['old'], ' \n  ')

    def test_added_whitespace_node_is_explained(self):
        result = compare_html(page('<p>A</p><p>B</p>'), page('<p>A</p>\n  <p>B</p>'), {**BASE, 'normalize_whitespace': True})
        self.assertFalse(result['changes'])
        self.assertEqual(len(result['suppressed']), 1)
        self.assertEqual(result['suppressed'][0]['old'], None)
        self.assertEqual(result['suppressed'][0]['new'], '\n  ')
        self.assertEqual(result['suppressed'][0]['normalized'], '')

    def test_substantive_whitespace_normalized_text_still_changes(self):
        result = compare_html(page('<p>A  B</p>'), page('<p>A C</p>'), {**BASE, "normalize_whitespace": True})
        self.assertEqual((result["changes"][0]["old"], result["changes"][0]["new"]), ("A B", "A C"))
        self.assertEqual(result["suppressed"], [])
        self.assertTrue(any(e["old"] != e["new"] for e in result["normalizations"]["before"]))

    def test_pre_code_textarea_and_scripts_preserve_whitespace(self):
        for tag in ('pre', 'code', 'textarea', 'script', 'style'):
            with self.subTest(tag=tag):
                result = compare_html(page(f'<{tag}> a  b </{tag}>'), page(f'<{tag}>a b</{tag}>'), {**BASE, "normalize_whitespace": True})
                self.assertTrue(result["changes"])

    def test_outside_region_explicitly_not_compared(self):
        result = compare_html('<title>old</title>'+page('x'), '<title>new</title>'+page('x'), BASE)
        self.assertFalse(result["changes"])
        self.assertEqual(result["scope"]["outside_region"], "not compared")

    def test_escaped_key_paths(self):
        result = compare_html(collection(item('a/b~c')), collection(item('a/b~c', '$12')), keyed_rules())
        self.assertIn('a~1b~0c', result["changes"][0]["path"])

    def test_empty_collection_can_be_region_descendant(self):
        self.assertFalse(compare_html(collection(), collection(), keyed_rules())["changes"])

    def test_collection_can_be_region(self):
        rules = {**keyed_rules(), "required_region": ".items"}
        self.assertFalse(compare_html(collection(item()), collection(item()), rules)["changes"])

    def test_examples_and_experiment_labels(self):
        from examples.page_diff.experiment import run
        result = run()
        self.assertEqual(result["totals"]["missed"], 0)
        self.assertEqual(result["totals"]["unexpected"], 0)
        self.assertEqual(result["totals"]["retained"], 6)
        result = compare_files(EXAMPLES / 'before.html', EXAMPLES / 'after.html', load_rules(EXAMPLES / 'rules.json'))
        self.assertFalse(result["changes"])
        self.assertEqual({e["rule"] for e in result["suppressed"]}, {"normalize_whitespace", "ignore_selectors", "volatile_attributes", "unordered_collection"})


class ContractTests(unittest.TestCase):
    def assert_invalid(self, html, rules=BASE, message=None):
        with self.assertRaisesRegex(ContractError, message or '.'):
            compare_html(page('<p>valid</p>'), html, rules)

    def test_required_region_missing_multiple_or_blank(self):
        for html in ('', '  ', '<html></html>', '<main></main>', '<main> </main>', '<main><!--only comment--></main>', '<main>x</main><main>y</main>', '<main>\x00x</main>'):
            with self.subTest(html=html):
                self.assert_invalid(html)

    def test_before_is_validated_too(self):
        with self.assertRaisesRegex(ContractError, 'before'):
            compare_html('<main></main>', page('x'), BASE)

    def test_duplicate_html_attributes_fail_instead_of_losing_values(self):
        with self.assertRaisesRegex(ContractError, 'duplicate HTML attribute'):
            compare_html(page('<p id="a" ID="b">x</p>'), page('x'), BASE)

    def test_required_subselector_missing_or_empty(self):
        for html in (page('<p>no price</p>'), page('<p class="price"></p>')):
            self.assert_invalid(html, {**BASE, "required_selectors": [".price"]})

    def test_ignored_only_content_and_ignored_required_selector_fail(self):
        for html, rules in [('<main><p class="noise">only text</p></main>', {**BASE, "ignore_selectors": [".noise"]}), (page('<p>x</p>'), {**BASE, "required_selectors": ["h1"], "ignore_selectors": ["h1"]})]:
            with self.assertRaises(ContractError):
                compare_html(html, html, rules)

    def test_duplicate_and_missing_keys_even_on_ignored_item(self):
        for html in (collection(item(), item()), collection('<article>x</article>'), collection('<article data-id=" ">x</article>')):
            with self.assertRaises(ContractError):
                compare_html(html, html, keyed_rules())

    def test_missing_collection_and_ambiguous_collection(self):
        for html in (page('x'), page('<section class="items"></section><section class="items"></section>')):
            with self.assertRaises(ContractError):
                compare_html(html, html, keyed_rules())

    def test_mixed_or_nested_collection_rejected(self):
        for html in (page('<section class="items"><h2>heading</h2>'+item()+'</section>'), page('<section class="items"><div>'+item()+'</div></section>'), page('<section class="items">stray'+item()+'</section>')):
            with self.assertRaises(ContractError):
                compare_html(html, html, keyed_rules())
        html = page('<section class="outer"><article data-id="o"><section class="inner"><article data-id="i">text</article></section></article></section>')
        rules = {**BASE, "unordered_collections": [{"selector": ".outer", "items": ":scope > article", "key_attribute": "data-id"}, {"selector": ".inner", "items": "article", "key_attribute": "data-id"}]}
        with self.assertRaisesRegex(ContractError, 'nested'):
            compare_html(html, html, rules)

    def test_overlapping_collection_rules(self):
        rules = keyed_rules()
        rules['unordered_collections'].append({"selector": "section", "items": "article", "key_attribute": "data-id"})
        with self.assertRaisesRegex(ContractError, 'overlapping'):
            compare_html(collection(item()), collection(item()), rules)

    def test_ignored_collection_root_or_item_rejected(self):
        for selector in ('main', '.items', 'article'):
            with self.assertRaises(ContractError):
                compare_html(collection(item()), collection(item()), keyed_rules(ignore_selectors=[selector]))

    def test_key_attribute_cannot_be_volatile(self):
        rules = keyed_rules(volatile_attributes=[{"selector": "article", "attributes": ["data-id"]}])
        with self.assertRaisesRegex(ContractError, 'key attribute'):
            compare_html(collection(item()), collection(item()), rules)

    def test_config_validation(self):
        invalid = [None, [], {}, {**BASE, "version": True}, {**BASE, "version": 2}, {**BASE, "oops": 1}, {**BASE, "required_region": "["}, {**BASE, "required_region": ":unknown"}, {**BASE, "normalize_whitespace": "false"}, {**BASE, "ignore_selectors": "p"}, {**BASE, "required_selectors": [None]}, {**BASE, "volatile_attributes": [{"selector": "p", "attributes": []}]}, {**BASE, "volatile_attributes": [{"selector": "p", "attributes": ["ID"]}]}, {**BASE, "volatile_attributes": [{"selector": "p", "attributes": ["id", "id"]}]}, {**BASE, "unordered_collections": [{"selector": "p", "items": "[", "key_attribute": "id"}]}, {**BASE, "unordered_collections": [{"selector": "p", "items": "p", "key_attribute": []}]}]
        for rules in invalid:
            with self.subTest(rules=rules), self.assertRaises(ContractError):
                validate_rules(rules)

    def test_rules_not_mutated(self):
        rules = keyed_rules()
        previous = deepcopy(rules)
        compare_html(collection(item()), collection(item()), rules)
        self.assertEqual(rules, previous)


class RuleCoverageTests(unittest.TestCase):
    def test_selector_drift_gates_on_either_side_and_when_stale_on_both(self):
        matched = page('<p class="clock">10:00</p>')
        stale = page('<p class="time">10:01</p>')
        for field, entry in [('ignore_selectors', '.clock'),
                             ('volatile_attributes', {'selector': '.clock', 'attributes': ['data-render']})]:
            for before, after, side in [(matched, stale, 'after'), (stale, matched, 'before'), (stale, stale, 'before')]:
                with self.subTest(field=field, side=side):
                    with self.assertRaises(ContractError) as raised:
                        compare_html(before, after, {**BASE, field: [entry]})
                    message = str(raised.exception)
                    for detail in (side, f'{field}[0]', '.clock', 'expected at least 1', 'got 0'):
                        self.assertIn(detail, message)

    def test_coverage_counts_nested_overlapping_and_unchanged_rules(self):
        html = page('<aside class="noise"><em class="inner">nested</em></aside><p data-render="x">keep</p>')
        rules = {**BASE, 'ignore_selectors': ['.noise', '.inner', '.noise'],
                 'volatile_attributes': [{'selector': 'p', 'attributes': ['data-render', 'title']},
                                         {'selector': '[data-render]', 'attributes': ['data-render']}]}
        report = compare_html(html, html, rules)
        coverage = report['rule_coverage']
        self.assertEqual(coverage['before'], coverage['after'])
        self.assertEqual([r['rule_id'] for r in coverage['before']],
                         ['ignore_selectors[0]', 'ignore_selectors[1]', 'ignore_selectors[2]',
                          'volatile_attributes[0]', 'volatile_attributes[1]'])
        self.assertEqual([r['matched_elements'] for r in coverage['before']], [1] * 5)
        self.assertEqual(coverage['before'][3]['attributes_present'], {'data-render': 1, 'title': 0})
        self.assertEqual(coverage['before'][4]['attributes_present'], {'data-render': 1})
        self.assertEqual(report['changes'], [])
        self.assertEqual(report['suppressed'], [])
        self.assertEqual(len(report['normalizations']['before']), 3)

    def test_optional_zero_match_rules_are_visible(self):
        rules = {**BASE, 'ignore_selectors': [{'selector': 'aside', 'min_matches': 0}],
                 'volatile_attributes': [{'selector': 'aside', 'attributes': ['id'], 'min_matches': 0}]}
        report = compare_html(page('keep'), page('keep'), rules)
        self.assertEqual([r['matched_elements'] for r in report['rule_coverage']['before']], [0, 0])
        self.assertEqual([r['min_matches'] for r in report['rule_coverage']['before']], [0, 0])
        self.assertEqual(report['rule_coverage']['before'][1]['attributes_present'], {'id': 0})

    def test_minimum_match_count_is_enforced(self):
        for field, entry in [('ignore_selectors', {'selector': 'p', 'min_matches': 2}),
                             ('volatile_attributes', {'selector': 'p', 'attributes': ['id'], 'min_matches': 2})]:
            with self.subTest(field=field), self.assertRaisesRegex(ContractError, 'expected at least 2.*got 1'):
                compare_html(page('<p>one</p>'), page('<p>one</p>'), {**BASE, field: [entry]})

    def test_volatile_coverage_is_checked_after_ignores(self):
        html = page('<aside><p data-render="x">noise</p></aside>')
        with self.assertRaisesRegex(ContractError, r'before:.*volatile_attributes\[0\].*got 0'):
            compare_html(html, html, {**BASE, 'ignore_selectors': ['aside'],
                                     'volatile_attributes': [{'selector': 'p', 'attributes': ['data-render']}]})

    def test_root_matching_and_attribute_counts_do_not_require_attributes(self):
        rules = {**BASE, 'volatile_attributes': [{'selector': 'main', 'attributes': ['id']}]}
        report = compare_html(page('keep'), page('keep'), rules)
        self.assertEqual(report['rule_coverage']['before'][0]['matched_elements'], 1)
        self.assertEqual(report['rule_coverage']['before'][0]['attributes_present'], {'id': 0})

    def test_collection_and_whitespace_coverage(self):
        html = collection(item())
        report = compare_html(html, html, keyed_rules(normalize_whitespace=True))
        rows = report['rule_coverage']['before']
        self.assertEqual(rows[0]['rule_id'], 'unordered_collections[0]')
        self.assertEqual(rows[0]['matched_elements'], 1)
        self.assertEqual(rows[0]['item_count'], 1)
        self.assertEqual(rows[1], {'rule_id': 'normalize_whitespace', 'text_nodes': 2, 'changed_text_nodes': 0,
                                   'matched_nodes': [{'original_path': '/region/h1[0]/#text[0]'},
                                                     {'original_path': '/region/section[0]/article[0]/p[0]/#text[0]'}]})
        empty = compare_html(collection(), collection(), keyed_rules())
        self.assertEqual(empty['rule_coverage']['before'][0]['item_count'], 0)

    def test_owner_and_expiry_are_preserved_without_mutating_rules(self):
        metadata = {'owner': 'catalog-team', 'expires': '2099-01-01'}
        rules = keyed_rules(ignore_selectors=[{'selector': '.optional', 'min_matches': 0, **metadata}],
                            volatile_attributes=[{'selector': 'article', 'attributes': ['title'], **metadata}])
        rules['unordered_collections'][0].update(metadata)
        original = deepcopy(rules)
        with patch('qawrench.page_diff.core._utc_today', return_value=date(2026, 11, 11)):
            report = compare_html(collection(item()), collection(item()), rules)
        self.assertEqual(report['rule_coverage']['evaluated_on'], '2026-11-11')
        for row in report['rule_coverage']['before']:
            self.assertEqual(row['owner'], 'catalog-team')
            self.assertEqual(row['expires'], '2099-01-01')
        self.assertEqual(rules, original)
        self.assertEqual(validate_rules(report['rules']), report['rules'])
        with self.assertRaisesRegex(ContractError, r"before:.*unordered_collections\[0\].*got 0.*catalog-team"):
            compare_html(page('keep'), collection(item()), rules)

    def test_whitespace_coverage_counts_only_eligible_surviving_text(self):
        html = page('<aside>ignored</aside><p>  A  B </p><pre>  protected  </pre>')
        report = compare_html(html, html, {**BASE, 'ignore_selectors': ['aside'], 'normalize_whitespace': True})
        self.assertEqual(report['rule_coverage']['before'][-1],
                         {'rule_id': 'normalize_whitespace', 'text_nodes': 2, 'changed_text_nodes': 1,
                          'matched_nodes': [{'original_path': '/region/h1[0]/#text[0]'},
                                            {'original_path': '/region/p[0]/#text[0]'}]})

    def test_no_normalization_rules_have_empty_coverage(self):
        report = compare_html(page('keep'), page('keep'), BASE)
        self.assertEqual(report['rule_coverage']['before'], [])
        self.assertEqual(report['rule_coverage']['after'], [])

    def test_expiry_gates_at_start_of_utc_date_even_for_optional_rules(self):
        entries = [('ignore_selectors', {'selector': '.optional', 'min_matches': 0}),
                   ('volatile_attributes', {'selector': 'p', 'attributes': ['id']}),
                   ('unordered_collections', {'selector': '.items', 'items': 'article', 'key_attribute': 'data-id'})]
        for field, entry in entries:
            rules = {**BASE, field: [{**entry, 'owner': 'catalog-team', 'expires': '2026-11-12'}]}
            with self.subTest(field=field), patch('qawrench.page_diff.core._utc_today', return_value=date(2026, 11, 11)):
                compare_html(collection(item()), collection(item()), rules)
            for today in (date(2026, 11, 12), date(2026, 11, 13)):
                with self.subTest(field=field, today=today), patch('qawrench.page_diff.core._utc_today', return_value=today):
                    with self.assertRaises(ContractError) as raised:
                        compare_html(collection(item()), collection(item()), rules)
                    for detail in ('expired', f'{field}[0]', entry['selector'], 'catalog-team', '2026-11-12', today.isoformat()):
                        self.assertIn(detail, str(raised.exception))

    def test_coverage_and_metadata_validation(self):
        invalid = [{'selector': 'p', 'min_matches': value} for value in (True, -1, 1.5, '1', None)]
        invalid += [{'selector': 'p', 'owner': value} for value in ('', '  ', 1, None)]
        invalid += [{'selector': 'p', 'expires': value} for value in ('2026-02-30', '20261112', '2026-11-12T00:00:00Z', '', 1, None)]
        invalid += [{'selector': 'p', 'oops': True}]
        for entry in invalid:
            for field in ('ignore_selectors', 'volatile_attributes', 'unordered_collections'):
                value = dict(entry)
                if field == 'volatile_attributes':
                    value['attributes'] = ['id']
                elif field == 'unordered_collections':
                    value.update(items='article', key_attribute='data-id')
                with self.subTest(field=field, entry=value), self.assertRaises(ContractError):
                    validate_rules({**BASE, field: [value]})
