"""Named OpenGraph floors use real pinned extruct and offline synthetic captures."""

import copy
import json

from qawrench.structured_data import ParityError, make_baseline
from . import test_baseline
from .test_parity import FixtureCase, document


class OpenGraphMinimumTests(FixtureCase):
    run_cli = test_baseline.BaselineTests.run_cli

    def setUp(self):
        super().setUp()
        self.baseline = self.directory / 'accepted.json'

    def pair(self, left, right=None):
        self.old.write_text(left)
        self.new.write_text(left if right is None else right)

    def test_unrelated_property_cannot_satisfy_initial_title_requirement(self):
        text = document(meta='<meta property="og:image" content="mug.png">')
        report = self.compare(text, text, syntaxes=('opengraph',))
        # Existing aggregate counts alone accept the wrong metadata.
        make_baseline(report, minimum_coverage={'opengraph.selected': 1})
        code, out, err = self.run_cli('--syntax', 'opengraph', '--save-baseline', str(self.baseline),
                                      '--minimum-og-property', 'og:title', '1')
        self.assertEqual(code, 2)
        self.assertTrue(out, err)
        error = json.loads(out)['errors'][0]
        self.assertEqual(error['code'], 'minimum_coverage')
        self.assertFalse(self.baseline.exists())
        self.assertEqual([p['capture'] for p in error['provenance']], ['old', 'new'])
        for source in error['provenance']:
            self.assertEqual(source['unmet'], [{'property': 'og:title', 'required': 1, 'observed': 0}])
            self.assertEqual(source['coverage']['opengraph']['selected'], 1)
            self.assertIn('selector', source['coverage']['opengraph'])
            self.assertEqual(source['coverage']['opengraph']['property_counts'], {'og:image': 1})

    def test_save_replay_and_same_total_property_replacement(self):
        left = document({'@id': 'x'}, meta='<meta property="og:title" content="A">')
        self.pair(left, left.replace('content="A"', 'content="B"'))
        code, out, _ = self.run_cli('--save-baseline', str(self.baseline), '--fail-on-diff',
                                  '--minimum-og-property', 'og:title', '1',
                                  '--minimum-coverage', 'json-ld.entities', '1')
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)['change_count'], 1)
        saved = json.loads(self.baseline.read_text())
        self.assertEqual(saved['minimum_og_properties'], {'og:title': 1})
        self.assertNotIn('minimum_og_properties', saved['policy'])
        code, out, _ = self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['accepted_change_count'], 1)
        before = self.baseline.read_bytes()
        self.new.write_text(left.replace('og:title', 'og:image'))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)['errors'][0]['code'], 'minimum_coverage')
        self.assertEqual(self.baseline.read_bytes(), before)

    def test_both_captures_preserve_baseline_and_error_output(self):
        title = document(meta='<meta property="og:title" content="A">')
        image = title.replace('og:title', 'og:image')
        self.pair(title)
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline))[0], 0)
        before = self.baseline.read_bytes()
        output = self.directory / 'error.json'
        for left, right, failed in [(image, title, 'old'), (title, image, 'new')]:
            with self.subTest(failed=failed):
                self.pair(left, right)
                code, _, _ = self.run_cli('--save-baseline', str(self.baseline), '--out', str(output),
                                          '--minimum-og-property', 'og:title', '1')
                self.assertEqual(code, 2)
                report = json.loads(output.read_text())
                self.assertEqual((report['status'], report['changes']), ('error', []))
                self.assertEqual(report['errors'][0]['code'], 'minimum_coverage')
                self.assertEqual([p['capture'] for p in report['errors'][0]['provenance']], [failed])
                self.assertEqual(self.baseline.read_bytes(), before)

    def test_repeated_values_count_separately_and_custom_namespaces_work(self):
        text = document(meta='<meta property="og:title" content="A">'
                             '<meta property="og:title" content="A">'
                             '<meta property="custom:label" content="B">').replace(
                                 '<html>', '<html prefix="custom: https://fixture.test/ns#">')
        report = self.compare(text, text, syntaxes=('opengraph',))
        requirements = {'og:title': 2, 'custom:label': 1}
        saved = make_baseline(report, minimum_og_properties=requirements)
        self.assertEqual(saved['minimum_og_properties'], requirements)
        requirements['og:title'] = 99
        self.assertEqual(saved['minimum_og_properties']['og:title'], 2)
        with self.assertRaises(ParityError) as raised:
            make_baseline(report, minimum_og_properties={'og:title': 3})
        self.assertEqual(raised.exception.code, 'minimum_coverage')
        self.assertEqual(raised.exception.provenance[0]['unmet'],
                         [{'property': 'og:title', 'required': 3, 'observed': 2}])
        self.baseline.write_text(json.dumps(saved))
        self.assertEqual(self.run_cli('--syntax', 'opengraph', '--baseline', str(self.baseline))[0], 0)

    def test_skipped_and_case_mismatched_properties_do_not_satisfy_floor(self):
        for meta, body, skipped in [
            ('<meta name="og:title" content="A">', '', 'name_attribute'),
            ('<meta property="og:title">', '', 'missing_content'),
            ('', '<meta property="og:title" content="A">', 'outside_head'),
            ('<meta property="unknown:title" content="A">', '', 'unknown_namespace'),
            ('<meta property="og:Title" content="A">', '', None),
        ]:
            with self.subTest(meta=meta, body=body):
                text = document(meta=meta, body=body)
                report = self.compare(text, text)
                property_name = 'unknown:title' if 'unknown' in meta else 'og:title'
                with self.assertRaises(ParityError) as raised:
                    make_baseline(report, minimum_og_properties={property_name: 1})
                self.assertEqual(raised.exception.code, 'minimum_coverage')
                coverage = raised.exception.provenance[0]['coverage']['opengraph']
                if skipped:
                    self.assertEqual(coverage['skipped'][0]['reason'], skipped)
                    self.assertTrue(coverage['skipped'][0]['line'])

    def test_combined_minimums_have_deterministic_unmet_entries(self):
        report = self.compare(document(), document())
        with self.assertRaises(ParityError) as raised:
            make_baseline(report, minimum_coverage={'json-ld.entities': 1},
                          minimum_og_properties={'og:title': 1, 'og:image': 2})
        self.assertEqual(raised.exception.provenance[0]['unmet'], [
            {'metric': 'json-ld.entities', 'required': 1, 'observed': 0},
            {'property': 'og:image', 'required': 2, 'observed': 0},
            {'property': 'og:title', 'required': 1, 'observed': 0}])

    def test_invalid_library_requirements(self):
        report = self.compare(document(), document())
        invalid = [[], 'og:title', {1: 1}, {'': 1}, {' ': 1}, {' og:title': 1},
                   {'og:title ': 1}, {'og:title\n': 1}, {'og:ti tle': 1},
                   {'og:title': True}, {'og:title': 1.0}, {'og:title': '1'}, {'og:title': -1}]
        for requirements in invalid:
            with self.subTest(requirements=requirements), self.assertRaises(ParityError) as raised:
                make_baseline(report, minimum_og_properties=requirements)
            self.assertEqual(raised.exception.code, 'invalid_config')
        report = self.compare(document(), document(), syntaxes=('json-ld',))
        with self.assertRaises(ParityError) as raised:
            make_baseline(report, minimum_og_properties={'og:title': 0})
        self.assertEqual(raised.exception.code, 'invalid_config')

    def test_invalid_cli_requirements_preserve_baseline(self):
        self.pair(document())
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline))[0], 0)
        before = self.baseline.read_bytes()
        cases = [('--minimum-og-property', 'og:title', count) for count in ('-1', '1.0', 'true', 'bad')]
        cases += [('--minimum-og-property', name, '1') for name in ('', ' og:title', 'og:title ', 'og:title\n')]
        cases += [('--minimum-og-property', 'og:title', '1', '--minimum-og-property', 'og:title', '2'),
                  ('--syntax', 'json-ld', '--minimum-og-property', 'og:title', '0')]
        for args in cases:
            with self.subTest(args=args):
                code, out, _ = self.run_cli('--save-baseline', str(self.baseline), *args)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(out)['errors'][0]['code'], 'invalid_config')
                self.assertEqual(self.baseline.read_bytes(), before)
        for args in [(), ('--baseline', str(self.baseline))]:
            code, out, _ = self.run_cli(*args, '--minimum-og-property', 'og:title', '1')
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(out)['errors'][0]['code'], 'invalid_config')

    def test_optional_requirements_and_bad_saved_requirements(self):
        report = self.compare(document(), document())
        legacy = make_baseline(report)
        self.assertNotIn('minimum_og_properties', legacy)
        self.assertEqual(make_baseline(report, minimum_og_properties={}), legacy)
        for saved in [legacy, make_baseline(report, minimum_og_properties={'unknown:title': 0})]:
            self.baseline.write_text(json.dumps(saved))
            self.assertEqual(self.run_cli('--baseline', str(self.baseline))[0], 0)
        for requirements in [None, [], {'': 0}, {'og:title ': 0}, {'og:title': True},
                             {'og:title': 1.0}, {'og:title': -1}, {'og:title': 1}]:
            with self.subTest(requirements=requirements):
                saved = copy.deepcopy(legacy)
                saved['minimum_og_properties'] = requirements
                self.baseline.write_text(json.dumps(saved))
                code, out, _ = self.run_cli('--baseline', str(self.baseline))
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(out)['errors'][0]['code'], 'invalid_baseline')
        only = self.compare(document(), document(), syntaxes=('json-ld',))
        saved = make_baseline(only)
        saved['minimum_og_properties'] = {'og:title': 0}
        self.baseline.write_text(json.dumps(saved))
        code, out, _ = self.run_cli('--syntax', 'json-ld', '--baseline', str(self.baseline))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)['errors'][0]['code'], 'invalid_baseline')

    def test_markdown_retains_named_floor_diagnostics(self):
        self.pair(document())
        code, out, _ = self.run_cli('--save-baseline', str(self.baseline), '--format', 'markdown',
                                  '--minimum-og-property', 'og:title', '1')
        self.assertEqual(code, 2)
        for detail in ('minimum_coverage', 'og:title', 'property', 'required', 'observed', 'selector'):
            self.assertIn(detail, out)
