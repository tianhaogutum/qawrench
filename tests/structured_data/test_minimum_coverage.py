"""Offline baseline minimum contracts, using real pinned extruct selectors."""

import copy
import json

from qawrench.structured_data import ParityError, compare_files, make_baseline
from . import test_baseline
from .test_parity import BASE, FixtureCase, document


class MinimumCoverageTests(FixtureCase):
    run_cli = test_baseline.BaselineTests.run_cli
    save = test_baseline.BaselineTests.save

    def setUp(self):
        super().setUp()
        self.baseline = self.directory / 'accepted.json'
        self.left = document({'@id': 'x', 'price': '12.00'})
        self.right = document({'@id': 'x', 'price': '14.00'})

    def test_empty_initial_capture_regression(self):
        empty = document(body='<script type="Application/LD+JSON">{"@id":"x"}</script>',
                         meta='<meta name="og:title" content="ignored">')
        self.old.write_text(empty)
        self.new.write_text(empty)
        # Prior behavior really permits a baseline with zero eligible metadata.
        report = compare_files(self.old, self.new, base_url=BASE)
        self.assertEqual(make_baseline(report)['captures']['old']['coverage']['json-ld']['selected'], 0)
        code, out, _ = self.run_cli('--save-baseline', str(self.baseline),
                                  '--minimum-coverage', 'json-ld.selected', '1',
                                  '--minimum-coverage', 'opengraph.selected', '1')
        self.assertEqual(code, 2)
        error = json.loads(out)['errors'][0]
        self.assertEqual(error['code'], 'minimum_coverage')
        self.assertFalse(self.baseline.exists())
        self.assertEqual([p['capture'] for p in error['provenance']], ['old', 'new'])
        for source in error['provenance']:
            self.assertEqual(source['unmet'], [
                {'metric': 'json-ld.selected', 'required': 1, 'observed': 0},
                {'metric': 'opengraph.selected', 'required': 1, 'observed': 0}])
            self.assertEqual(source['coverage']['json-ld']['skipped'][0]['reason'], 'type_mismatch')
            self.assertEqual(source['coverage']['opengraph']['skipped'][0]['reason'], 'name_attribute')
            self.assertIn('selector', source['coverage']['json-ld'])
            self.assertTrue(source['coverage']['json-ld']['skipped'][0]['line'])

    def test_success_persists_requirements_replays_exact_deltas(self):
        meta = '<meta property="og:title" content="A">'
        self.old.write_text(document({'@id': 'x', 'price': '12.00'}, meta=meta))
        self.new.write_text(document({'@id': 'x', 'price': '14.00'}, meta=meta.replace('"A"', '"B"')))
        code, out, _ = self.run_cli('--save-baseline', str(self.baseline), '--fail-on-diff',
                                  '--minimum-coverage', 'json-ld.entities', '1',
                                  '--minimum-coverage', 'opengraph.selected', '1')
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)['change_count'], 2)
        saved = json.loads(self.baseline.read_text())
        self.assertEqual(saved['minimum_coverage'], {'json-ld.entities': 1, 'opengraph.selected': 1})
        self.assertNotIn('minimum_coverage', saved['policy'])
        code, out, _ = self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['accepted_change_count'], 2)
        # Existing reference/policy/floor guards still apply with requirements.
        self.new.write_text(document({'@id': 'x', 'price': '14.00'}))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)['errors'][0]['code'], 'minimum_coverage')
        self.new.write_text(document({'@id': 'x', 'price': '14.00'}, meta=meta))
        code, out, _ = self.run_cli('--baseline', str(self.baseline), '--syntax', 'json-ld')
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)['errors'][0]['code'], 'baseline_mismatch')
        self.old.write_bytes(self.old.read_bytes() + b'<!-- reference changed -->')
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)['errors'][0]['code'], 'baseline_mismatch')

    def test_both_captures_enforced_and_existing_baseline_preserved(self):
        self.save()
        before = self.baseline.read_bytes()
        output = self.directory / 'error.json'
        for left, right, failed in [(document(), document({'@id': 'x'}), 'old'),
                                    (document({'@id': 'x'}), document(), 'new')]:
            self.old.write_text(left)
            self.new.write_text(right)
            code, _, _ = self.run_cli('--save-baseline', str(self.baseline), '--out', str(output),
                                      '--minimum-coverage', 'json-ld.entities', '1')
            self.assertEqual(code, 2)
            report = json.loads(output.read_text())
            self.assertEqual((report['status'], report['changes']), ('error', []))
            error = report['errors'][0]
            self.assertEqual(error['code'], 'minimum_coverage')
            self.assertEqual(error['provenance'][0]['capture'], failed)
            self.assertEqual(self.baseline.read_bytes(), before)

    def test_selected_scripts_are_not_entity_coverage(self):
        report = self.compare(document(scripts=['[]']), document(scripts=['[]']), syntaxes=('json-ld',))
        saved = make_baseline(report, minimum_coverage={'json-ld.selected': 1})
        self.assertEqual(saved['minimum_coverage'], {'json-ld.selected': 1})
        with self.assertRaises(ParityError) as raised:
            make_baseline(report, minimum_coverage={'json-ld.entities': 1})
        self.assertEqual(raised.exception.code, 'minimum_coverage')
        self.assertEqual(raised.exception.provenance[0]['unmet'][0]['observed'], 0)

    def test_og_floors_count_eligible_values_including_repeats(self):
        meta = ('<meta property="og:title" content="A">'
                '<meta property="og:title" content="B">')
        report = self.compare(document(meta=meta), document(meta=meta), syntaxes=('opengraph',))
        make_baseline(report, minimum_coverage={'opengraph.selected': 2, 'opengraph.extracted_values': 2})
        with self.assertRaises(ParityError) as raised:
            make_baseline(report, minimum_coverage={'opengraph.selected': 3})
        self.assertEqual(raised.exception.provenance[0]['unmet'][0],
                         {'metric': 'opengraph.selected', 'required': 3, 'observed': 2})
        for skipped in ('<meta property="og:title">', '<meta property="unknown:title" content="A">',
                        '<meta name="og:title" content="A">'):
            with self.subTest(meta=skipped):
                report = self.compare(document(meta=skipped), document(meta=skipped), syntaxes=('opengraph',))
                with self.assertRaises(ParityError) as raised:
                    make_baseline(report, minimum_coverage={'opengraph.selected': 1})
                self.assertEqual(raised.exception.code, 'minimum_coverage')
        body = document(body='<meta property="og:title" content="A">')
        report = self.compare(body, body, syntaxes=('opengraph',))
        with self.assertRaises(ParityError) as raised:
            make_baseline(report, minimum_coverage={'opengraph.selected': 1})
        self.assertEqual(raised.exception.provenance[0]['coverage']['opengraph']['skipped'][0]['reason'],
                         'outside_head')

    def test_all_supported_metrics_and_selected_syntax_only(self):
        left = document({'@graph': [{'@id': 'x'}]}, meta='<meta property="og:title" content="A">')
        report = self.compare(left, left)
        requirements = {f'json-ld.{m}': 1 for m in ('selected', 'extracted_roots', 'entities', 'graph_wrappers')}
        requirements.update({f'opengraph.{m}': 1 for m in ('selected', 'extracted_values', 'blocks')})
        self.assertEqual(make_baseline(report, minimum_coverage=requirements)['minimum_coverage'], requirements)
        # The baseline owns its dict; later caller mutation must not change it.
        saved = make_baseline(report, minimum_coverage=requirements)
        requirements['json-ld.entities'] = 100
        self.assertEqual(saved['minimum_coverage']['json-ld.entities'], 1)
        for syntax, metric in [('json-ld', 'entities'), ('opengraph', 'selected')]:
            only = self.compare(left, left, syntaxes=(syntax,))
            self.assertEqual(make_baseline(only, minimum_coverage={f'{syntax}.{metric}': 1})['captures'], only['captures'])

    def test_invalid_library_requirements(self):
        report = self.compare(document({'@id': 'x'}), document({'@id': 'x'}), syntaxes=('json-ld',))
        invalid = [[], 'json-ld.entities', {'json-ld.entities': True}, {'json-ld.entities': 1.0},
                   {'json-ld.entities': '1'}, {'json-ld.entities': -1}, {1: 1},
                   {'json-ld.candidates': 1}, {'json-ld.entity:x': 1}, {'opengraph.selected': 0}]
        for requirements in invalid:
            with self.subTest(requirements=requirements), self.assertRaises(ParityError) as raised:
                make_baseline(report, minimum_coverage=requirements)
            self.assertEqual(raised.exception.code, 'invalid_config')

    def test_invalid_cli_requirements(self):
        self.save()
        before = self.baseline.read_bytes()
        cases = [('--minimum-coverage', 'json-ld.entities', count)
                 for count in ('-1', '1.0', 'true', 'invalid')]
        cases += [('--minimum-coverage', 'json-ld.candidates', '1'),
                  ('--minimum-coverage', 'json-ld.entities', '1', '--minimum-coverage', 'json-ld.entities', '2'),
                  ('--syntax', 'opengraph', '--minimum-coverage', 'json-ld.selected', '0')]
        for args in cases:
            with self.subTest(args=args):
                code, out, _ = self.run_cli('--save-baseline', str(self.baseline), *args)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(out)['errors'][0]['code'], 'invalid_config')
                self.assertEqual(self.baseline.read_bytes(), before)
        for args in [(), ('--baseline', str(self.baseline))]:
            code, out, _ = self.run_cli(*args, '--minimum-coverage', 'json-ld.entities', '1')
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(out)['errors'][0]['code'], 'invalid_config')

    def test_legacy_empty_and_explicit_zero_requirements(self):
        report = self.compare(document(), document())
        legacy = make_baseline(report)
        self.assertNotIn('minimum_coverage', legacy)
        self.assertEqual(make_baseline(report, minimum_coverage={}), legacy)
        for saved in [legacy, make_baseline(report, minimum_coverage={'json-ld.entities': 0})]:
            self.baseline.write_text(json.dumps(saved))
            result = compare_files(self.old, self.new, base_url=BASE, baseline=self.baseline)
            self.assertEqual(result['status'], 'equal')

    def test_bad_saved_requirements_and_saved_shortfalls(self):
        self.save()
        legacy = json.loads(self.baseline.read_text())
        for requirements in [None, [], {'unknown': 1}, {'json-ld.entities': True},
                             {'json-ld.entities': -1}, {'json-ld.entities': 2}]:
            value = copy.deepcopy(legacy)
            value['minimum_coverage'] = requirements
            with self.subTest(requirements=requirements):
                self.baseline.write_text(json.dumps(value))
                code, out, _ = self.run_cli('--baseline', str(self.baseline))
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(out)['errors'][0]['code'], 'invalid_baseline')

    def test_minimum_markdown_error_contains_diagnostics(self):
        self.old.write_text(document())
        self.new.write_text(document())
        code, out, _ = self.run_cli('--save-baseline', str(self.baseline), '--format', 'markdown',
                                  '--minimum-coverage', 'json-ld.entities', '1')
        self.assertEqual(code, 2)
        for detail in ('minimum_coverage', 'required', 'observed', 'selector'):
            self.assertIn(detail, out)
