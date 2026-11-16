"""Explicit sibling identities on synthetic mixed-content HTML."""
import json
from pathlib import Path
import unittest

from qawrench.page_diff import ContractError, compare_files, compare_html, load_rules, validate_rules

BASE = {'version': 1, 'required_region': 'main'}
RULES = {**BASE, 'sibling_identity': [{'selector': 'article', 'key_attribute': 'data-id'}]}
EXAMPLES = Path(__file__).resolve().parents[2] / 'examples' / 'page_diff' / 'identity'


def page(*blocks):
    return '<main><h1>Catalog</h1>' + ''.join(blocks) + '</main>'


def card(key, text='$10', attrs=''):
    key_attr = '' if key is None else f'data-id="{key}"'
    return f'<article {key_attr} {attrs}><p>{text}</p></article>'


class SiblingIdentityTests(unittest.TestCase):
    def test_reordered_blocks_keep_attribute_and_text_edits_with_original_paths(self):
        before = page(card('A', attrs='title="old"'), '<h2>More</h2>', card('B', '$20'))
        after = page(card('B', '$20'), '<h2>More</h2>', card('A', '$12', 'title="new"'))
        result = compare_html(before, after, RULES)
        edits = [c for c in result['changes'] if c['kind'] == 'changed']
        self.assertEqual([(c['old'], c['new']) for c in edits], [('old', 'new'), ('$10', '$12')])
        self.assertTrue(all('data-id="A"' in c['path'] for c in edits))
        self.assertEqual((edits[0]['before_path'], edits[0]['after_path']),
                         ('/region/article[0]/@title', '/region/article[1]/@title'))
        self.assertEqual((edits[1]['before_path'], edits[1]['after_path']),
                         ('/region/article[0]/p[0]/#text[0]', '/region/article[1]/p[0]/#text[0]'))
        moves = [c for c in result['changes'] if c['kind'] == 'moved']
        self.assertEqual(len(moves), 2)
        self.assertEqual(result['status'], 'changed')
        self.assertFalse(any(c['kind'] in ('removed', 'added') for c in result['changes']))

    def test_insertions_and_removals_do_not_move_surviving_blocks(self):
        for before, after, kind in [(page(card('A')), page(card('X'), card('A')), 'added'),
                                    (page(card('X'), card('A')), page(card('A')), 'removed')]:
            with self.subTest(kind=kind):
                changes = compare_html(before, after, RULES)['changes']
                self.assertEqual([c['kind'] for c in changes], [kind])
                self.assertIn('data-id="X"', changes[0]['path'])
                self.assertIsNone(changes[0]['before_path' if kind == 'added' else 'after_path'])

    def test_movement_across_edited_unkeyed_heading_is_retained(self):
        changes = compare_html(page(card('A'), '<h2>Old</h2>'),
                               page('<h2>New</h2>', card('A')), RULES)['changes']
        self.assertEqual(len([c for c in changes if c['kind'] == 'moved']), 1)
        self.assertEqual([(c['old'], c['new']) for c in changes if c['kind'] == 'changed'], [('Old', 'New')])

    def test_duplicate_keys_report_all_candidates_without_guessing(self):
        before = page(card('A'), card('A', '$20'), card('B'))
        after = page(card('B', '$11'), card('A', '$30'))
        result = compare_html(before, after, RULES)
        ambiguity = [c for c in result['changes'] if c['kind'] == 'ambiguous']
        self.assertEqual(len(ambiguity), 1)
        finding = ambiguity[0]
        self.assertEqual(finding['reason'], 'duplicate_key')
        self.assertEqual([c['path'] for c in finding['old']], ['/region/article[0]', '/region/article[1]'])
        self.assertEqual([c['path'] for c in finding['new']], ['/region/article[1]'])
        self.assertIn('$20', json.dumps(finding['old']))
        self.assertIn('$30', json.dumps(finding['new']))
        self.assertEqual([(c['old'], c['new']) for c in result['changes'] if c['kind'] == 'changed'], [('$10', '$11')])
        # Ambiguity must still be visible for identical snapshots.
        self.assertEqual(compare_html(before, before, RULES)['status'], 'changed')

    def test_missing_or_blank_keys_are_ambiguous_even_when_identical(self):
        for key in (None, '', '  '):
            with self.subTest(key=key):
                html = page(card(key))
                changes = compare_html(html, html, RULES)['changes']
                self.assertEqual([c['kind'] for c in changes], ['ambiguous'])
                self.assertEqual(changes[0]['reason'], 'missing_key')
                self.assertEqual(changes[0]['old'][0]['path'], '/region/article[0]')

    def test_identity_is_scoped_to_parent_and_rule(self):
        before = page('<section>' + card('A') + '</section><section>' + card('A', '$20') + '</section>')
        after = before.replace('$20', '$21')
        changes = compare_html(before, after, RULES)['changes']
        self.assertEqual([c['kind'] for c in changes], ['changed'])
        self.assertEqual(changes[0]['before_path'], '/region/section[1]/article[0]/p[0]/#text[0]')
        # Moving between parents remains remove/add; never match globally.
        before = page('<section>' + card('A') + '</section><section>Other</section>')
        after = page('<section></section><section>Other' + card('A') + '</section>')
        changes = compare_html(before, after, RULES)['changes']
        self.assertEqual(sorted(c['kind'] for c in changes), ['added', 'removed'])

    def test_changed_key_is_remove_add_and_keys_are_exact_escaped_strings(self):
        changes = compare_html(page(card('A')), page(card('a')), RULES)['changes']
        self.assertEqual(sorted(c['kind'] for c in changes), ['added', 'removed'])
        changes = compare_html(page(card('a/b~c')), page(card('a/b~c', '$12')), RULES)['changes']
        self.assertIn('a~1b~0c', changes[0]['path'])
        self.assertEqual(changes[0]['before_path'], '/region/article[0]/p[0]/#text[0]')

    def test_nested_selected_siblings_match_inside_moved_parent(self):
        rules = {**RULES, 'sibling_identity': RULES['sibling_identity'] + [{'selector': 'li', 'key_attribute': 'id'}]}
        a = '<article data-id="A"><ul><li id="x">old</li><li id="y">keep</li></ul></article>'
        changed = '<article data-id="A"><ul><li id="y">keep</li><li id="x">new</li></ul></article>'
        changes = compare_html(page(a, card('B')), page(card('B'), changed), rules)['changes']
        edit = next(c for c in changes if c['kind'] == 'changed')
        self.assertEqual((edit['old'], edit['new']), ('old', 'new'))
        self.assertEqual(edit['after_path'], '/region/article[1]/ul[0]/li[1]/#text[0]')

    def test_normalizations_follow_identity_and_locations_precede_ignores(self):
        rules = {**RULES, 'ignore_selectors': ['aside'], 'normalize_whitespace': True,
                 'volatile_attributes': [{'selector': 'article', 'attributes': ['data-render']}]}
        before = page('<aside>noise</aside>', card('A', '  $10 ', 'data-render="1"'), card('B', '$20', 'data-render="2"'))
        after = page(card('B', '$20', 'data-render="3"'), '<aside>new noise</aside>', card('A', ' $12 ', 'data-render="4"'))
        result = compare_html(before, after, rules)
        edit = next(c for c in result['changes'] if c['kind'] == 'changed')
        self.assertEqual((edit['old'], edit['new']), ('$10', '$12'))
        self.assertEqual(edit['before_path'], '/region/article[0]/p[0]/#text[0]')
        volatile = [e for e in result['suppressed'] if e['rule'] == 'volatile_attributes']
        self.assertEqual(sorted((e['old'], e['new']) for e in volatile), [('1', '4'), ('2', '3')])

    def test_ambiguous_normalizations_are_audited_without_claiming_matches(self):
        rules = {**RULES, 'volatile_attributes': [{'selector': 'article', 'attributes': ['title']}]}
        before = page(card('A', attrs='title="one"'), card('A', attrs='title="two"'))
        after = page(card('A', attrs='title="three"'), card('A', attrs='title="four"'))
        report = compare_html(before, after, rules)
        self.assertEqual(report['suppressed'], [])
        self.assertEqual(len(report['normalizations']['before']), 2)
        unique = page(card('A', attrs='title="unique"'))
        for a, b in ((before, unique), (unique, before)):
            with self.subTest(a=a):
                self.assertEqual(compare_html(a, b, rules)['suppressed'], [])

    def test_locations_survive_same_tag_ignore_whitespace_and_insertion(self):
        rules = {**BASE, 'sibling_identity': [{'selector': 'article.card', 'key_attribute': 'data-id'}],
                 'normalize_whitespace': True, 'ignore_selectors': ['.noise']}
        before = '<main>\n<article class="noise">noise</article>\n<article class="card" data-id="A"><p> $10 </p></article></main>'
        after = '<main><article class="card" data-id="X">new</article><article class="card" data-id="A"><p> $12 </p></article><article class="noise">noise</article></main>'
        changes = compare_html(before, after, rules)['changes']
        edit = next(c for c in changes if c['kind'] == 'changed')
        self.assertEqual((edit['old'], edit['new']), ('$10', '$12'))
        self.assertEqual(edit['before_path'], '/region/article[1]/p[0]/#text[0]')
        self.assertEqual(edit['after_path'], '/region/article[1]/p[0]/#text[0]')
        self.assertEqual(sorted(c['kind'] for c in changes), ['added', 'changed'])

    def test_distinct_rules_do_not_collide_and_zero_targets_are_allowed(self):
        rules = {**BASE, 'sibling_identity': [{'selector': '.one', 'key_attribute': 'data-id'},
                                            {'selector': '.two', 'key_attribute': 'data-id'}]}
        before = page(card('A', attrs='class="one"'), card('A', '$20', 'class="two"'))
        after = page(card('A', '$21', 'class="two"'), card('A', attrs='class="one"'))
        changes = compare_html(before, after, rules)['changes']
        self.assertEqual(len([c for c in changes if c['kind'] == 'moved']), 2)
        self.assertEqual([(c['old'], c['new']) for c in changes if c['kind'] == 'changed'], [('$20', '$21')])
        report = compare_html(page('no cards'), page('no cards'), rules)
        self.assertEqual(report['status'], 'unchanged')
        self.assertEqual([r['matched_elements'] for r in report['rule_coverage']['before']], [0, 0])

    def test_locations_also_cover_existing_keyed_collection_and_ordered_alignment(self):
        rules = {**BASE, 'unordered_collections': [{'selector': 'section', 'items': 'article', 'key_attribute': 'data-id'}]}
        before = page('<section>' + card('A') + card('B', '$20') + '</section>')
        after = page('<section>' + card('B', '$20') + card('A', '$12') + '</section>')
        edit = compare_html(before, after, rules)['changes'][0]
        self.assertEqual((edit['before_path'], edit['after_path']),
                         ('/region/section[0]/article[0]/p[0]/#text[0]', '/region/section[0]/article[1]/p[0]/#text[0]'))
        before = page('<p>keep</p><p>old</p>')
        after = page('<p>new first</p><p>keep</p><p>new</p>')
        edit = next(c for c in compare_html(before, after, BASE)['changes'] if c['kind'] == 'changed')
        self.assertEqual((edit['before_path'], edit['after_path']),
                         ('/region/p[1]/#text[0]', '/region/p[2]/#text[0]'))

    def test_no_rule_preserves_existing_ordered_behavior(self):
        before, after = page(card('A'), card('B')), page(card('B'), card('A', '$12'))
        changes = compare_html(before, after, BASE)['changes']
        self.assertTrue(changes)
        self.assertFalse(any(c['kind'] in ('moved', 'ambiguous') for c in changes))

    def test_bad_rules_and_conflicting_targets_fail(self):
        for entry in ({'selector': '[' , 'key_attribute': 'id'}, {'selector': 'p', 'key_attribute': 'ID'},
                      {'selector': 'p'}, {'selector': 'p', 'key_attribute': 'id', 'unknown': 1}):
            with self.subTest(entry=entry), self.assertRaises(ContractError):
                validate_rules({**BASE, 'sibling_identity': [entry]})
        for updates in ({'sibling_identity': [{'selector': 'main', 'key_attribute': 'id'}]},
                        {'sibling_identity': RULES['sibling_identity'] * 2},
                        {'ignore_selectors': ['article']},
                        {'volatile_attributes': [{'selector': 'article', 'attributes': ['data-id']}]},
                        {'unordered_collections': [{'selector': 'main', 'items': 'article', 'key_attribute': 'data-id'}]}):
            with self.subTest(updates=updates), self.assertRaises(ContractError):
                compare_html(page(card('A')), page(card('A')), {**RULES, **updates})

    def test_checked_in_fixture(self):
        report = compare_files(EXAMPLES / 'before.html', EXAMPLES / 'after.html', load_rules(EXAMPLES / 'rules.json'))
        self.assertEqual(len([c for c in report['changes'] if c['kind'] == 'changed']), 2)
        self.assertEqual(len([c for c in report['changes'] if c['kind'] == 'moved']), 2)
        self.assertEqual(report['status'], 'changed')
