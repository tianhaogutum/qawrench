"""Diagnostic candidate pairs never authorize a cross-profile comparison."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from examples.lighthouse.fixture_factory import AUDIT, lhr, policy
from qawrench.lighthouse import compare, create_baseline
from qawrench.lighthouse.core import _bounded_identity_changes, _identity_changes
from qawrench.lighthouse.report import markdown


class MismatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.b, self.c = self.root / 'b', self.root / 'c'
        self.b.mkdir()
        self.c.mkdir()
        self.p = policy()

    def add(self, side, name, *, value=2000, cpu=4, device='mobile', edit=None):
        doc = lhr(value)
        doc['configSettings']['formFactor'] = device
        doc['configSettings']['throttling']['cpuSlowdownMultiplier'] = cpu
        if edit:
            edit(doc)
        for index in range(7):
            (side / f'{name}-{index}.json').write_text(json.dumps(doc))

    def scenario(self):
        self.add(self.b, 'mobile')
        self.add(self.c, 'mobile', value=2500)
        self.add(self.b, 'desktop', device='desktop')
        self.add(self.c, 'desktop', device='desktop', cpu=8)

    def test_cpu_mismatch_has_structured_fields_without_comparing_profiles(self):
        self.scenario()
        report = compare(self.b, self.c, self.p, confidence_level=.95)
        self.assertEqual((report['decision'], report['exit_code']), ('invalid', 2))
        self.assertEqual(len(report['mismatches']), 1)
        pair = report['mismatches'][0]
        by_id = {g['id']: g for g in report['groups']}
        baseline, current = (by_id[pair[key]] for key in ('baseline_group_id', 'current_group_id'))
        self.assertEqual(pair, {
            'baseline_group_id': baseline['id'], 'current_group_id': current['id'],
            'finalUrl': 'https://synthetic.example.test/',
            'finalDisplayedUrl': 'https://synthetic.example.test/', 'formFactor': 'desktop',
            'changes': [{'path': '/configSettings/throttling/cpuSlowdownMultiplier',
                         'baseline_present': True, 'baseline': 4,
                         'current_present': True, 'current': 8}]})
        for group, counts in ((baseline, (7, 0)), (current, (0, 7))):
            self.assertEqual(group['decision'], 'invalid')
            metric = group['metrics'][0]
            self.assertEqual((metric['baseline']['count'], metric['current']['count']), counts)
            self.assertEqual(metric['decision'], 'invalid')
            self.assertIsNone(metric['delta'])
            self.assertIsNone(metric['confidence']['delta_interval'])
        mobile = next(g for g in report['groups'] if g['identity']['configSettings']['formFactor'] == 'mobile')
        self.assertEqual(mobile['decision'], 'regression')
        self.assertEqual(mobile['metrics'][0]['confidence']['delta_interval'], [500, 500])
        text = markdown(report).split('## Complete evidence')[0]
        self.assertIn('## Profile mismatches', text)
        self.assertIn('/configSettings/throttling/cpuSlowdownMultiplier | 4 | 8 |', text)
        self.assertIn(baseline['id'], text)
        self.assertIn(current['id'], text)

    def test_browser_and_nested_config_differences_are_lossless(self):
        def baseline(d):
            d['configSettings']['custom'] = {
                'removed': {}, 'nullToAbsent': None, 'scalar': {'x': 1},
                'list': [1, 2], 'same': {'a': 1, 'b': 2},
                'typed': False, 'representation': 4, 'a/b~c': 'old|value`'}

        def current(d):
            d['userAgent'] = 'OtherChrome'
            d['environment'].update(hostUserAgent='OtherHost', networkUserAgent='OtherNetwork')
            d['configSettings']['custom'] = {
                'added': {}, 'absentToNull': None, 'scalar': 0,
                'list': [2, 1], 'same': {'b': 2, 'a': 1},
                'typed': 0, 'representation': 4.0, 'a/b~c': 'new\nvalue'}

        self.add(self.b, 'b', edit=baseline)
        self.add(self.c, 'c', edit=current)
        report = compare(self.b, self.c, self.p)
        changes = report['mismatches'][0]['changes']
        expected = [
            ('/configSettings/custom/a~1b~0c', True, 'old|value`', True, 'new\nvalue'),
            ('/configSettings/custom/absentToNull', False, None, True, None),
            ('/configSettings/custom/added', False, None, True, {}),
            ('/configSettings/custom/list', True, [1, 2], True, [2, 1]),
            ('/configSettings/custom/nullToAbsent', True, None, False, None),
            ('/configSettings/custom/removed', True, {}, False, None),
            ('/configSettings/custom/representation', True, 4, True, 4.0),
            ('/configSettings/custom/scalar', True, {'x': 1}, True, 0),
            ('/configSettings/custom/typed', True, False, True, 0),
            ('/hostUserAgent', True, 'SyntheticChrome/136.0.0.0', True, 'OtherHost'),
            ('/networkUserAgent', True, 'SyntheticMobile/136.0.0.0', True, 'OtherNetwork'),
            ('/userAgent', True, 'SyntheticChrome/136.0.0.0', True, 'OtherChrome')]
        self.assertEqual(changes, [dict(zip(
            ['path', 'baseline_present', 'baseline', 'current_present', 'current'], row)) for row in expected])
        text = markdown(report).split('## Complete evidence')[0]
        self.assertIn('/configSettings/custom/nullToAbsent | null | (missing) |', text)
        self.assertIn('/configSettings/custom/absentToNull | (missing) | null |', text)
        self.assertIn('old\\|value`', text)
        self.assertIn('new\\nvalue', text)

    def test_whole_added_config_and_reordered_list_are_reported(self):
        self.add(self.b, 'b')
        self.add(self.c, 'c', edit=lambda d: d['configSettings'].update(custom={'nested': []}, output=['html', 'json']))
        changes = compare(self.b, self.c, self.p)['mismatches'][0]['changes']
        self.assertEqual([c['path'] for c in changes], ['/configSettings/custom', '/configSettings/output'])
        self.assertFalse(changes[0]['baseline_present'])
        self.assertEqual(changes[0]['current'], {'nested': []})
        self.assertEqual(changes[1]['baseline'], ['json'])
        self.assertEqual(changes[1]['current'], ['html', 'json'])

    def test_ambiguous_profiles_list_all_pairs_and_exclude_matched_groups(self):
        for side in (self.b, self.c):
            self.add(side, 'matched')
        for cpu in (6, 8):
            self.add(self.b, str(cpu), cpu=cpu)
        for cpu in (10, 12):
            self.add(self.c, str(cpu), cpu=cpu)
        report = compare(self.b, self.c, self.p)
        pairs = report['mismatches']
        self.assertEqual(len(pairs), 4)
        ids = [(p['baseline_group_id'], p['current_group_id']) for p in pairs]
        self.assertEqual(ids, sorted(set(ids)))
        self.assertEqual({(p['changes'][0]['baseline'], p['changes'][0]['current']) for p in pairs},
                         {(6, 10), (6, 12), (8, 10), (8, 12)})
        matched = next(g for g in report['groups'] if g['decision'] == 'pass')
        self.assertTrue(all(matched['id'] not in pair for pair in ids))
        self.assertEqual((report['decision'], report['exit_code']), ('invalid', 2))

    def test_exact_urls_and_form_factor_are_required(self):
        self.add(self.b, 'b')
        edits = [lambda d: d.update(finalUrl='https://other.example.test/'),
                 lambda d: d.update(finalUrl='https://synthetic.example.test'),
                 lambda d: d.update(finalDisplayedUrl='https://synthetic.example.test/#fragment'),
                 lambda d: d['configSettings'].update(formFactor='desktop')]
        for edit in edits:
            with self.subTest(edit=edit):
                self.add(self.c, 'c', cpu=8, edit=edit)
                report = compare(self.b, self.c, self.p)
                self.assertEqual(report['mismatches'], [])
                self.assertEqual((report['decision'], report['exit_code']), ('invalid', 2))

    def test_matched_key_reordering_and_unassignable_inputs_have_no_candidates(self):
        self.add(self.b, 'b')
        self.add(self.c, 'c', edit=lambda d: d.update(configSettings=dict(reversed(list(d['configSettings'].items())))))
        report = compare(self.b, self.c, self.p)
        self.assertEqual(report['mismatches'], [])
        self.assertEqual(report['decision'], 'pass')
        self.assertNotIn('## Profile mismatches', markdown(report))
        self.add(self.c, 'c', edit=lambda d: d['configSettings'].pop('formFactor'))
        (self.c / 'broken.json').write_text('{')
        report = compare(self.b, self.c, self.p, confidence_level=.95)
        self.assertEqual(report['mismatches'], [])
        self.assertEqual(report['decision'], 'invalid')
        for directory in (self.b, self.c):
            for path in directory.glob('*.json'):
                path.unlink()
        self.assertEqual(compare(self.b, self.c, self.p)['mismatches'], [])

    def test_manifest_modes_and_repeated_reports_preserve_pairs(self):
        self.scenario()
        self.p['metrics'][AUDIT]['relative_tolerance'] = 0
        saved = self.root / 'baseline.json'
        saved.write_text(json.dumps(create_baseline(self.b, self.p)))
        for options in ({}, {'confidence_level': .95}, {'confidence_level': .95, 'confidence_gate': True}):
            with self.subTest(options=options):
                direct = compare(self.b, self.c, self.p, **options)
                manifest = compare(saved, self.c, self.p, **options)
                self.assertEqual(manifest['mismatches'], direct['mismatches'])
                self.assertEqual(manifest['groups'], direct['groups'])
                self.assertEqual(len(manifest['mismatches']), 1)
                self.assertEqual((manifest['decision'], manifest['exit_code']), ('invalid', 2))
                repeated = compare(saved, self.c, self.p, **options)
                self.assertEqual(json.dumps(manifest, sort_keys=True), json.dumps(repeated, sort_keys=True))
                self.assertEqual(markdown(manifest), markdown(repeated))

    def ambiguous_scenario(self):
        self.add(self.b, 'mobile')
        self.add(self.c, 'mobile', value=2500)
        for cpu in (4, 6, 8):
            self.add(self.b, f'desktop-{cpu}', device='desktop', cpu=cpu)
        for cpu in (10, 12, 14, 16):
            self.add(self.c, f'desktop-{cpu}', device='desktop', cpu=cpu)
        self.add(self.b, 'unrelated', edit=lambda d: d.update(finalUrl='https://unrelated.example.test/'))

    def test_limit_emits_prefix_without_diffing_omitted_pairs(self):
        self.ambiguous_scenario()
        unlimited = compare(self.b, self.c, self.p, confidence_level=.95)
        self.assertEqual(len(unlimited['mismatches']), 12)
        self.assertNotIn('mismatch_summary', unlimited)
        with patch('qawrench.lighthouse.core._identity_changes', wraps=_identity_changes) as diff:
            limited = compare(self.b, self.c, self.p, confidence_level=.95, mismatch_limit=5)
        self.assertEqual(diff.call_count, 5)
        self.assertEqual({k: v for k, v in limited.pop('mismatch_summary').items() if k != 'groups'},
                         {'limit': 5, 'total': 12, 'emitted': 5, 'omitted': 7, 'truncated': True})
        self.assertEqual(limited.pop('mismatches'), unlimited.pop('mismatches')[:5])
        self.assertEqual(limited, unlimited)  # All group/run/decision/confidence evidence survives.
        mobile = next(g for g in limited['groups'] if g['identity']['configSettings']['formFactor'] == 'mobile'
                      and g['decision'] == 'regression')
        self.assertEqual(mobile['metrics'][0]['confidence']['delta_interval'], [500, 500])
        self.assertEqual((limited['decision'], limited['exit_code']), ('invalid', 2))

    def test_limit_counts_pairs_across_anchors_in_global_order(self):
        self.ambiguous_scenario()
        for side, cpus in ((self.b, (4, 6)), (self.c, (8,))):
            for cpu in cpus:
                self.add(side, f'other-{cpu}', cpu=cpu,
                         edit=lambda d: d.update(finalUrl='https://other.example.test/',
                                                finalDisplayedUrl='https://other.example.test/'))
        unlimited = compare(self.b, self.c, self.p)
        self.assertEqual(len(unlimited['mismatches']), 14)  # 3*4 + 2*1, unrelated contributes zero.
        for limit in (1, 5, 13, 14, 20):
            with self.subTest(limit=limit):
                report = compare(self.b, self.c, self.p, mismatch_limit=limit)
                self.assertEqual(report['mismatches'], unlimited['mismatches'][:limit])
                emitted = min(14, limit)
                self.assertEqual({k: v for k, v in report['mismatch_summary'].items() if k != 'groups'}, {'limit': limit, 'total': 14,
                    'emitted': emitted, 'omitted': 14-emitted, 'truncated': emitted < 14})
                self.assertEqual(report['groups'], unlimited['groups'])

    def test_limit_zero_candidates_and_default_compatibility(self):
        self.add(self.b, 'b')
        self.add(self.c, 'c')
        original = compare(self.b, self.c, self.p)
        self.assertEqual(original, compare(self.b, self.c, self.p, mismatch_limit=None))
        for edit in (None, lambda d: d.update(finalDisplayedUrl='https://other.example.test/'),
                     lambda d: d['configSettings'].update(formFactor='desktop'),
                     lambda d: d['configSettings'].pop('formFactor')):
            with self.subTest(edit=edit):
                self.add(self.c, 'c', edit=edit)
                unlimited = compare(self.b, self.c, self.p)
                limited = compare(self.b, self.c, self.p, mismatch_limit=5)
                self.assertEqual(limited['mismatches'], [])
                self.assertEqual({k: v for k, v in limited.pop('mismatch_summary').items() if k != 'groups'},
                                 {'limit': 5, 'total': 0, 'emitted': 0, 'omitted': 0, 'truncated': False})
                self.assertEqual(limited, unlimited)

    def test_limit_manifest_modes_markdown_and_lossless_changes(self):
        self.ambiguous_scenario()
        self.p['metrics'][AUDIT]['relative_tolerance'] = 0
        saved = self.root / 'saved.json'
        saved.write_text(json.dumps(create_baseline(self.b, self.p)))
        for options in ({}, {'confidence_level': .95}, {'confidence_level': .95, 'confidence_gate': True}):
            with self.subTest(options=options):
                direct = compare(self.b, self.c, self.p, mismatch_limit=5, **options)
                manifest = compare(saved, self.c, self.p, mismatch_limit=5, **options)
                for key in ('mismatches', 'mismatch_summary', 'groups', 'decision', 'exit_code'):
                    self.assertEqual(manifest[key], direct[key])
                self.assertEqual(manifest, compare(saved, self.c, self.p, mismatch_limit=5, **options))
                text = markdown(manifest).split('## Complete evidence')[0]
                self.assertIn('total **12**, emitted **5**, omitted **7**', text)
                self.assertIn('limit 5; truncated: true', text)
                self.assertIn('/configSettings/throttling/cpuSlowdownMultiplier', text)
        # The cap counts pairs, never fields: missing/null, lists and objects stay whole.
        for directory in (self.b, self.c):
            for path in directory.glob('*.json'):
                path.unlink()
        self.add(self.b, 'b', edit=lambda d: d['configSettings'].update(custom={'gone': None, 'list': [1, 2]}))
        self.add(self.c, 'c', edit=lambda d: d['configSettings'].update(custom={'added': {}, 'list': [2, 1]}))
        limited = compare(self.b, self.c, self.p, mismatch_limit=1)
        self.assertEqual(limited['mismatches'], compare(self.b, self.c, self.p)['mismatches'])
        self.assertEqual(len(limited['mismatches'][0]['changes']), 3)

    def assert_group_accounting(self, report, unlimited):
        rows = report['mismatch_summary']['groups']
        unmatched = {g['id']: ('baseline' if g['metrics'][0]['baseline']['run_count'] else 'current')
                     for g in report['groups']
                     if not g['metrics'][0]['baseline']['run_count'] or not g['metrics'][0]['current']['run_count']}
        self.assertEqual([row['group_id'] for row in rows], sorted(unmatched))
        for row in rows:
            key = row['side'] + '_group_id'
            total = sum(pair[key] == row['group_id'] for pair in unlimited['mismatches'])
            emitted = sum(pair[key] == row['group_id'] for pair in report['mismatches'])
            self.assertEqual(row, {'group_id': row['group_id'], 'side': unmatched[row['group_id']],
                                  'total': total, 'emitted': emitted, 'omitted': total-emitted,
                                  'truncated': emitted < total})
        for side in ('baseline', 'current'):
            for key in ('total', 'emitted', 'omitted'):
                self.assertEqual(sum(row[key] for row in rows if row['side'] == side),
                                 report['mismatch_summary'][key])

    def test_group_accounting_ambiguity_manifest_modes_and_markdown(self):
        self.ambiguous_scenario()
        self.p['metrics'][AUDIT]['relative_tolerance'] = 0
        saved = self.root / 'saved.json'
        saved.write_text(json.dumps(create_baseline(self.b, self.p)))
        for options in ({}, {'confidence_level': .95}, {'confidence_level': .95, 'confidence_gate': True}):
            with self.subTest(options=options):
                unlimited = compare(self.b, self.c, self.p, **options)
                with patch('qawrench.lighthouse.core._identity_changes', wraps=_identity_changes) as diff:
                    report = compare(self.b, self.c, self.p, mismatch_limit=5, **options)
                self.assertEqual(diff.call_count, 5)
                self.assert_group_accounting(report, unlimited)
                rows = report['mismatch_summary']['groups']
                self.assertEqual(len(rows), 8)  # matched mobile excluded; unrelated baseline included
                self.assertEqual(sorted(row['total'] for row in rows), [0, 3, 3, 3, 3, 4, 4, 4])
                self.assertTrue(any(row['total'] and not row['emitted'] and row['truncated'] for row in rows))
                manifest = compare(saved, self.c, self.p, mismatch_limit=5, **options)
                for key in ('groups', 'mismatches', 'mismatch_summary', 'decision', 'exit_code'):
                    self.assertEqual(manifest[key], report[key])
                self.assertEqual(report['mismatches'], unlimited['mismatches'][:5])
                self.assertEqual({k: v for k, v in report.items() if k not in ('mismatches', 'mismatch_summary')},
                                 {k: v for k, v in unlimited.items() if k != 'mismatches'})
                self.assertEqual((report['decision'], report['exit_code']), ('invalid', 2))
                text = markdown(report).split('## Complete evidence')[0]
                self.assertIn('| Unmatched group | Side | Total candidates | Emitted | Omitted | Status |', text)
                for row in rows:
                    status = 'no exact-anchor candidates' if not row['total'] else ('truncated' if row['truncated'] else 'complete')
                    self.assertIn(f"| {row['group_id']} | {row['side']} | {row['total']} | {row['emitted']} | {row['omitted']} | {status} |", text)
                self.assertNotIn('| Unmatched group |', markdown(unlimited))
                historical = json.loads(json.dumps(report))
                del historical['mismatch_summary']['groups']
                historical_text = markdown(historical).split('## Complete evidence')[0]
                self.assertIn('total **12**, emitted **5**, omitted **7**', historical_text)
                self.assertNotIn('| Unmatched group |', historical_text)

    def test_group_accounting_multi_anchor_cap_equality_and_above_total(self):
        self.ambiguous_scenario()
        for side, cpus in ((self.b, (4, 6)), (self.c, (8,))):
            for cpu in cpus:
                self.add(side, f'other-{cpu}', cpu=cpu,
                         edit=lambda d: d.update(finalUrl='https://other.example.test/',
                                                finalDisplayedUrl='https://other.example.test/'))
        self.add(self.c, 'isolated-current', edit=lambda d: d.update(finalDisplayedUrl='https://alone.example.test/'))
        unlimited = compare(self.b, self.c, self.p)
        self.assertEqual(len(unlimited['mismatches']), 14)
        for limit in (1, 5, 13, 14, 20):
            with self.subTest(limit=limit):
                report = compare(self.b, self.c, self.p, mismatch_limit=limit)
                self.assert_group_accounting(report, unlimited)
                self.assertEqual(report['mismatches'], unlimited['mismatches'][:limit])
                self.assertEqual(sum(row['total'] == 0 for row in report['mismatch_summary']['groups']), 2)
                self.assertEqual(report['groups'], unlimited['groups'])
                if limit >= 14:
                    self.assertTrue(all(not row['truncated'] and row['omitted'] == 0
                                        for row in report['mismatch_summary']['groups']))

    def test_group_accounting_zero_and_invalid_identity_boundaries(self):
        self.add(self.b, 'b')
        edits = [(None, 0), (lambda d: d.update(finalUrl='https://other.example.test/'), 2),
                 (lambda d: d.update(finalDisplayedUrl='https://other.example.test/'), 2),
                 (lambda d: d['configSettings'].update(formFactor='desktop'), 2),
                 (lambda d: d['configSettings'].pop('formFactor'), 1)]
        for edit, count in edits:
            with self.subTest(edit=edit):
                self.add(self.c, 'c', edit=edit)
                unlimited = compare(self.b, self.c, self.p)
                report = compare(self.b, self.c, self.p, mismatch_limit=5)
                self.assert_group_accounting(report, unlimited)
                self.assertEqual(len(report['mismatch_summary']['groups']), count)
                self.assertEqual(report['mismatch_summary']['total'], 0)
                self.assertEqual(report['groups'], unlimited['groups'])
        # A known identity with bad metric data keeps existing candidate eligibility.
        self.add(self.c, 'c', cpu=8, edit=lambda d: d['audits'][AUDIT].update(numericUnit='unitless'))
        unlimited = compare(self.b, self.c, self.p, confidence_level=.95)
        report = compare(self.b, self.c, self.p, mismatch_limit=1, confidence_level=.95)
        self.assert_group_accounting(report, unlimited)
        self.assertEqual([row['total'] for row in report['mismatch_summary']['groups']], [1, 1])
        self.assertEqual(report['groups'], unlimited['groups'])
        self.assertEqual((report['decision'], report['exit_code']), ('invalid', 2))
        for directory in (self.b, self.c):
            for path in directory.glob('*.json'):
                path.unlink()
        report = compare(self.b, self.c, self.p, mismatch_limit=5)
        self.assertEqual(report['mismatch_summary']['groups'], [])
        self.assertEqual((report['decision'], report['exit_code']), ('invalid', 2))

    def field_scenario(self):
        self.add(self.b, 'mobile')
        self.add(self.c, 'mobile', value=2500)

        def baseline(d):
            d['configSettings']['custom'] = {
                'removed': {}, 'nullToAbsent': None, 'scalar': {'x': 1},
                'list': [1, 2], 'typed': False, 'representation': 4, 'a/b~c': 'old'}

        def current(d):
            d['configSettings']['custom'] = {
                'added': {}, 'absentToNull': None, 'scalar': 0,
                'list': [2, 1], 'typed': 0, 'representation': 4.0, 'a/b~c': 'new'}
            d['userAgent'] = 'OtherChrome'
            d['environment'].update(hostUserAgent='OtherHost', networkUserAgent='OtherNetwork')

        self.add(self.b, 'desktop', device='desktop', edit=baseline)
        self.add(self.c, 'desktop', device='desktop', edit=current)

    def test_field_limit_prefix_counts_manifest_and_confidence_modes(self):
        self.field_scenario()
        self.p['metrics'][AUDIT]['relative_tolerance'] = 0
        saved = self.root / 'saved.json'
        saved.write_text(json.dumps(create_baseline(self.b, self.p)))
        for options in ({}, {'confidence_level': .95}, {'confidence_level': .95, 'confidence_gate': True}):
            for pair_limit in (None, 1):
                with self.subTest(options=options, pair_limit=pair_limit):
                    full = compare(self.b, self.c, self.p, mismatch_limit=pair_limit, **options)
                    self.assertEqual(len(full['mismatches'][0]['changes']), 12)
                    self.assertEqual(full, compare(self.b, self.c, self.p, mismatch_limit=pair_limit,
                                                  mismatch_field_limit=None, **options))
                    for cap in (1, 5, 12, 20):
                        limited = compare(self.b, self.c, self.p, mismatch_limit=pair_limit,
                                          mismatch_field_limit=cap, **options)
                        pair = limited['mismatches'][0]
                        emitted = min(12, cap)
                        self.assertEqual(pair['changes'], full['mismatches'][0]['changes'][:cap])
                        self.assertEqual(pair.pop('changes_summary'), dict(limit=cap, total=12, emitted=emitted,
                                         omitted=12-emitted, truncated=emitted < 12))
                        pair['changes'] = full['mismatches'][0]['changes']
                        self.assertEqual(limited, full)  # Every non-field datum is unchanged.
                    direct = compare(self.b, self.c, self.p, mismatch_limit=pair_limit,
                                     mismatch_field_limit=5, **options)
                    manifest = compare(saved, self.c, self.p, mismatch_limit=pair_limit,
                                       mismatch_field_limit=5, **options)
                    self.assertEqual({k:v for k,v in manifest.items() if k != 'baseline_manifest'}, direct)
                    self.assertEqual(manifest, compare(saved, self.c, self.p, mismatch_limit=pair_limit,
                                                       mismatch_field_limit=5, **options))
                    text = markdown(direct).split('## Complete evidence')[0]
                    self.assertIn('| Baseline group | Current group | Total changes | Emitted | Omitted | Limit | Truncated |', text)
                    pair = direct['mismatches'][0]
                    self.assertIn(f"| {pair['baseline_group_id']} | {pair['current_group_id']} | 12 | 5 | 7 | 5 | true |", text)
                    self.assertNotIn('| Total changes |', markdown(full))
                    self.assertEqual((direct['decision'], direct['exit_code']), ('invalid', 2))

    def test_field_limit_combines_with_candidate_prefix_and_zero_candidates(self):
        self.ambiguous_scenario()
        full = compare(self.b, self.c, self.p, mismatch_limit=5)
        limited = compare(self.b, self.c, self.p, mismatch_limit=5, mismatch_field_limit=1)
        self.assertEqual(limited['mismatch_summary'], full['mismatch_summary'])
        self.assertEqual(limited['groups'], full['groups'])
        self.assertEqual(len(limited['mismatches']), 5)
        for pair, original in zip(limited['mismatches'], full['mismatches']):
            self.assertEqual(pair.pop('changes_summary'), dict(limit=1, total=1, emitted=1, omitted=0, truncated=False))
            self.assertEqual(pair, original)
        for path in self.c.glob('*.json'):
            path.unlink()
        self.add(self.c, 'unrelated', edit=lambda d: d.update(finalUrl='https://elsewhere.example.test/'))
        full = compare(self.b, self.c, self.p, mismatch_limit=5)
        self.assertEqual(full['mismatches'], [])
        self.assertEqual(compare(self.b, self.c, self.p, mismatch_limit=5, mismatch_field_limit=5), full)
        self.assertNotIn('| Total changes |', markdown(full))

    def test_field_limit_counts_atomic_changes_and_raw_key_order_without_full_list(self):
        baseline = {'a/': None, 'a0': {'gone': {}}, 'list': [1, 2], 'scalar': {'x': 1}, 'typed': False}
        current = {'a/': {}, 'a0': {'added': None}, 'list': [2, 1], 'scalar': 0, 'typed': 0}
        full = _identity_changes(baseline, current)
        # Raw-key traversal order differs from sorting the escaped pointer strings.
        self.assertEqual([row['path'] for row in full], ['/a~1', '/a0/added', '/a0/gone', '/list', '/scalar', '/typed'])
        for cap in (1, 5, 6, 20):
            changes, summary = _bounded_identity_changes(baseline, current, cap)
            self.assertEqual(changes, full[:cap])
            emitted = min(cap, 6)
            self.assertEqual(summary, dict(limit=cap, total=6, emitted=emitted, omitted=6-emitted, truncated=emitted<6))
        changes, summary = _bounded_identity_changes(baseline, baseline, 5)
        self.assertEqual(changes, [])
        self.assertEqual(summary, dict(limit=5, total=0, emitted=0, omitted=0, truncated=False))
        # Bounded mode must not invoke the helper that materializes every change.
        self.field_scenario()
        with patch('qawrench.lighthouse.core._identity_changes', side_effect=AssertionError('unbounded diff')):
            report = compare(self.b, self.c, self.p, mismatch_field_limit=5)
        self.assertEqual(report['mismatches'][0]['changes_summary']['total'], 12)
        # An omitted candidate must not be diffed, even for field totals.
        self.ambiguous_scenario()
        with patch('qawrench.lighthouse.core._bounded_identity_changes', wraps=_bounded_identity_changes) as diff:
            report = compare(self.b, self.c, self.p, mismatch_limit=1, mismatch_field_limit=1)
        self.assertEqual(diff.call_count, 1)
        self.assertEqual(len(report['mismatches']), 1)

    def test_field_limit_rejects_bad_options_and_preserves_cli_outputs(self):
        for cap in (0, -1, True, False, 1.0, .5, '5', [], {}, float('inf'), float('nan')):
            with self.subTest(cap=cap), self.assertRaisesRegex(ValueError, 'mismatch_field_limit'):
                compare(self.b, self.c, self.p, mismatch_field_limit=cap)
        config = self.root / 'gate.json'
        config.write_text(json.dumps(self.p))
        out, md = self.root / 'report.json', self.root / 'report.md'
        out.write_text('preserve JSON')
        md.write_text('preserve Markdown')
        for cap in ('0', '-1', '1.0', 'true', 'no', ''):
            with self.subTest(cli=cap):
                result = subprocess.run([sys.executable, '-m', 'qawrench.lighthouse', str(self.b), str(self.c),
                    '--config', str(config), '--mismatch-field-limit', cap, '--out', str(out), '--markdown', str(md)],
                    capture_output=True, text=True, cwd=Path(__file__).resolve().parents[2], timeout=15)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('mismatch', result.stderr)
                self.assertEqual(out.read_text(), 'preserve JSON')
                self.assertEqual(md.read_text(), 'preserve Markdown')

    def test_markdown_summary_retains_tables_manifest_modes_and_zero_candidates(self):
        self.field_scenario()
        self.p['metrics'][AUDIT]['relative_tolerance'] = 0
        saved = self.root / 'saved.json'
        saved.write_text(json.dumps(create_baseline(self.b, self.p)))
        for options in ({}, {'confidence_level': .95}, {'confidence_level': .95, 'confidence_gate': True}):
            for baseline in (self.b, saved):
                for caps in ({}, {'mismatch_limit': 1, 'mismatch_field_limit': 5}):
                    with self.subTest(options=options, baseline=baseline, caps=caps):
                        report = compare(baseline, self.c, self.p, **options, **caps)
                        before = json.dumps(report, sort_keys=True)
                        complete = markdown(report)
                        self.assertEqual(complete, markdown(report, include_evidence=True))
                        with patch('qawrench.lighthouse.report.json.dumps', wraps=json.dumps) as dumps:
                            summary = markdown(report, include_evidence=False)
                        self.assertFalse(any(call.args[0] is report for call in dumps.call_args_list))
                        prefix = complete.split('\n## Complete evidence')[0]
                        self.assertTrue(summary.startswith(prefix + '\n'))
                        self.assertNotIn('## Complete evidence', summary)
                        self.assertNotIn('```json', summary)
                        self.assertIn('use the JSON report', summary)
                        self.assertIn('## Metric decisions', summary)
                        if caps:
                            self.assertIn('| 12 | 5 | 7 | 5 | true |', summary)
                        if options:
                            self.assertIn('## Confidence summaries', summary)
                            self.assertIn('[500, 500]', summary)
                        self.assertEqual(json.dumps(report, sort_keys=True), before)
                        self.assertEqual((report['decision'], report['exit_code']), ('invalid', 2))
        # Zero candidates: fully matched and unrelated exact anchors, capped and unlimited.
        for directory in (self.b, self.c):
            for path in directory.glob('*.json'):
                path.unlink()
        self.add(self.b, 'b')
        for edit in (None, lambda d: d.update(finalUrl='https://elsewhere.example.test/')):
            self.add(self.c, 'c', edit=edit)
            for caps in ({}, {'mismatch_limit': 5, 'mismatch_field_limit': 5}):
                report = compare(self.b, self.c, self.p, **caps)
                summary = markdown(report, include_evidence=False)
                self.assertTrue(summary.startswith(markdown(report).split('\n## Complete evidence')[0] + '\n'))
                self.assertIn('use the JSON report', summary)
                if caps:
                    self.assertIn('total **0**, emitted **0**, omitted **0**', summary)
                    if edit:
                        self.assertIn('no exact-anchor candidates', summary)

    def test_markdown_summary_requires_boolean_library_option(self):
        report = compare(self.b, self.c, self.p)
        for value in (None, 0, 1, 0.0, 'false', [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'include_evidence'):
                markdown(report, include_evidence=value)

    def test_markdown_summary_cli_json_parity_dry_run_and_rejections(self):
        self.ambiguous_scenario()
        self.p['metrics'][AUDIT]['relative_tolerance'] = 0
        config = self.root / 'gate.json'
        config.write_text(json.dumps(self.p))
        out, md = self.root / 'report.json', self.root / 'report.md'
        command = [sys.executable, '-m', 'qawrench.lighthouse', str(self.b), str(self.c),
                   '--config', str(config), '--mismatch-limit', '5', '--mismatch-field-limit', '1',
                   '--confidence-level', '.95', '--confidence-gate']

        def run(*args):
            return subprocess.run(command + list(args), capture_output=True, text=True,
                cwd=Path(__file__).resolve().parents[2], timeout=15)

        self.assertEqual(run('--out', str(out), '--markdown', str(md)).returncode, 2)
        full_json, full_md = out.read_bytes(), md.read_text()
        result = run('--out', str(out), '--markdown', str(md), '--markdown-summary')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(out.read_bytes(), full_json)
        summary = md.read_text()
        self.assertTrue(summary.startswith(full_md.split('\n## Complete evidence')[0] + '\n'))
        self.assertIn('total **12**, emitted **5**, omitted **7**', summary)
        self.assertIn('no exact-anchor candidates', summary)
        self.assertEqual(summary, markdown(json.loads(full_json), include_evidence=False))
        result = run('--out', str(out), '--markdown', str(md), '--markdown-summary', '--dry-run')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout.encode(), full_json)
        self.assertEqual(out.read_bytes(), full_json)
        self.assertEqual(md.read_text(), summary)
        for args in (['--markdown-summary'], ['--markdown-summary', '--dry-run'],
                     ['--markdown', str(md), '--markdown-summary', 'false'],
                     ['--markdown', str(md), '--markdown-summary', '--mismatch-field-limit', '0'],
                     ['--markdown', str(md), '--markdown-summary', '--confidence-level', '2'],
                     ['--markdown', str(self.root / 'absent' / 'report.md'), '--markdown-summary']):
            with self.subTest(args=args):
                result = run('--out', str(out), *args)
                self.assertEqual(result.returncode, 2)
                self.assertTrue(result.stderr)
                self.assertEqual(out.read_bytes(), full_json)
                self.assertEqual(md.read_text(), summary)

    def test_limit_rejects_bad_library_and_cli_values_without_replacing_outputs(self):
        for limit in (0, -1, True, False, 1.0, .5, '5', [], {}, float('inf'), float('nan')):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, 'mismatch_limit'):
                compare(self.b, self.c, self.p, mismatch_limit=limit)
        config = self.root / 'gate.json'
        config.write_text(json.dumps(self.p))
        out, md = self.root / 'report.json', self.root / 'report.md'
        out.write_text('preserve JSON')
        md.write_text('preserve Markdown')
        for limit in ('0', '-1', '1.0', 'true', 'no', ''):
            with self.subTest(cli=limit):
                result = subprocess.run([sys.executable, '-m', 'qawrench.lighthouse', str(self.b), str(self.c),
                    '--config', str(config), '--mismatch-limit', limit, '--out', str(out), '--markdown', str(md)],
                    capture_output=True, text=True, cwd=Path(__file__).resolve().parents[2], timeout=15)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('mismatch', result.stderr)
                self.assertEqual(out.read_text(), 'preserve JSON')
                self.assertEqual(md.read_text(), 'preserve Markdown')


if __name__ == '__main__':
    unittest.main()
