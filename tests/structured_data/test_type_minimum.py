"""Entity-specific literal JSON-LD types are opt-in and use raw extraction."""

import copy
import json

from qawrench.structured_data import ParityError, make_baseline
from . import test_baseline
from .test_parity import FixtureCase, document


class TypeMinimumTests(FixtureCase):
    run_cli = test_baseline.BaselineTests.run_cli

    def setUp(self):
        super().setUp()
        self.baseline = self.directory / 'accepted.json'
        self.required = 'https://fixture.test/product'

    def test_right_identity_wrong_type_cannot_satisfy_requirement(self):
        text = document({'@id': '../product', '@type': 'Thing'})
        report = self.compare(text, text, syntaxes=('json-ld',))
        # Presence succeeds; a required type must be a distinct guard.
        make_baseline(report, required_jsonld_entities=[self.required])
        code, out, err = self.run_cli('--syntax', 'json-ld', '--save-baseline', str(self.baseline),
                                     '--require-jsonld-type', self.required, 'Product')
        self.assertEqual(code, 2)
        self.assertTrue(out, err)
        error = json.loads(out)['errors'][0]
        self.assertEqual(error['code'], 'minimum_coverage')
        self.assertFalse(self.baseline.exists())
        self.assertEqual([p['capture'] for p in error['provenance']], ['old', 'new'])
        for source in error['provenance']:
            self.assertEqual(source['unmet'], [{'entity': self.required, 'type': 'Product',
                                               'required': 1, 'observed': 0, 'observed_types': ['Thing']}])
            self.assertEqual(source['coverage']['json-ld']['entity_types'], {self.required: ['Thing']})
            self.assertIn('selector', source['coverage']['json-ld'])

    def test_save_replay_multiple_types_and_same_identity_type_loss(self):
        text = document({'@id': '../product', '@type': ['Product', 'Thing', 'Product'], 'name': 'A'},
                        meta='<meta property="og:title" content="A">')
        report = self.compare(text, text.replace('"name": "A"', '"name": "B"'))
        self.assertEqual(report['captures']['old']['coverage']['json-ld']['entity_types'],
                         {self.required: ['Product', 'Thing']})
        flags = ('--require-jsonld-type', self.required, 'Thing',
                 '--require-jsonld-type', self.required, 'Product',
                 '--require-jsonld-entity', self.required, '--minimum-coverage', 'json-ld.entities', '1',
                 '--minimum-og-property', 'og:title', '1', '--minimum-og-nonblank-property', 'og:title', '1')
        code, out, _ = self.run_cli('--save-baseline', str(self.baseline), '--fail-on-diff', *flags)
        self.assertEqual(code, 1)
        saved = json.loads(self.baseline.read_text())
        self.assertEqual(saved['required_jsonld_types'], {self.required: ['Product', 'Thing']})
        self.assertNotIn('required_jsonld_types', saved['policy'])
        code, out, _ = self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['accepted_change_count'], 1)
        before = self.baseline.read_bytes()
        self.new.write_text(text.replace('"Product"', '"Service"'))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)['errors'][0]['code'], 'minimum_coverage')
        self.assertEqual(json.loads(out)['errors'][0]['provenance'][0]['unmet'][0]['observed_types'],
                         ['Service', 'Thing'])
        self.assertEqual(self.baseline.read_bytes(), before)

    def test_each_initial_side_and_error_output_preserve_existing_baseline(self):
        good = document({'@id': '../product', '@type': 'Product'})
        wrong = document({'@id': '../product'})
        self.compare(good, good)
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline))[0], 0)
        before = self.baseline.read_bytes()
        output = self.directory / 'error.json'
        for left, right, failed in [(wrong, good, 'old'), (good, wrong, 'new')]:
            with self.subTest(failed=failed):
                self.compare(left, right)
                code, _, _ = self.run_cli('--save-baseline', str(self.baseline), '--out', str(output),
                                          '--require-jsonld-type', self.required, 'Product')
                self.assertEqual(code, 2)
                error = json.loads(output.read_text())
                self.assertEqual((error['status'], error['changes']), ('error', []))
                self.assertEqual(error['errors'][0]['code'], 'minimum_coverage')
                self.assertEqual([p['capture'] for p in error['errors'][0]['provenance']], [failed])
                self.assertEqual(self.baseline.read_bytes(), before)

    def test_literal_types_do_not_coerce_expand_or_rewrite_comparison_values(self):
        for value in [None, '', ' ', 12, True, {}, {'@id': 'Product'}, ['Product', None],
                      ['Product', ''], [['Product']], [], 'product', 'https://schema.org/Product', ' Product ']:
            with self.subTest(value=value):
                text = document({'@id': '../product', '@type': value})
                report = self.compare(text, text)
                with self.assertRaises(ParityError) as raised:
                    make_baseline(report, required_jsonld_types={self.required: ['Product']})
                self.assertEqual(raised.exception.code, 'minimum_coverage')
        text = document({'@context': {'kind': '@type'}, '@id': '../product', 'kind': 'Product',
                         'reference': {'@id': '../other', '@type': 'Product'}})
        with self.assertRaises(ParityError):
            make_baseline(self.compare(text, text), required_jsonld_types={self.required: ['Product']})
        left = document({'@id': '../product', '@type': ['Thing', 'Product', 'Product']})
        right = document({'@id': '../product', '@type': ['Product', 'Thing']})
        report = self.compare(left, right)
        make_baseline(report, required_jsonld_types={self.required: ['Product', 'Thing']})
        self.assertEqual([(c['kind'], c['path']) for c in report['changes']],
                         [('changed', '/@type/0'), ('changed', '/@type/1'), ('removed', '/@type/2')])
        # Raw coverage is independent of opt-in comparison normalization.
        text = document({'@id': '../product', '@type': ' Product '})
        report = self.compare(text, text, normalize={'/@type': 'trim'})
        with self.assertRaises(ParityError):
            make_baseline(report, required_jsonld_types={self.required: ['Product']})
        make_baseline(report, required_jsonld_types={self.required: [' Product ']})

    def test_resolved_custom_blank_node_and_graph_entity_keys(self):
        text = document({'@graph': [{'@id': '../product', '@type': 'Product'},
                                   {'@id': '_:node', '@type': 'Thing'}]})
        report = self.compare(text, text)
        requirements = {self.required: ['Product'], '_:node': ['Thing']}
        saved = make_baseline(report, required_jsonld_types=requirements)
        requirements[self.required].append('Missing')
        self.assertEqual(saved['required_jsonld_types'][self.required], ['Product'])
        self.assertEqual(list(saved['required_jsonld_types']), sorted(requirements))
        self.baseline.write_text(json.dumps(saved))
        self.assertEqual(self.run_cli('--baseline', str(self.baseline))[0], 0)
        with self.assertRaises(ParityError):
            make_baseline(report, required_jsonld_types={'../product': ['Product']})
        text = document({'sku': ' SKU 12 ', '@id': '../product', '@type': 'Product'})
        report = self.compare(text, text, identity_field='sku')
        saved = make_baseline(report, required_jsonld_types={' SKU 12 ': ['Product']})
        self.baseline.write_text(json.dumps(saved))
        self.assertEqual(self.run_cli('--identity-field', 'sku', '--baseline', str(self.baseline))[0], 0)
        with self.assertRaises(ParityError):
            make_baseline(report, required_jsonld_types={'SKU 12': ['Product']})

    def test_missing_unrelated_and_skipped_entities_do_not_satisfy_types(self):
        for text in [document({'@id': '../other', '@type': 'Product'}), document({'@graph': []}),
                     document({'@id': '../product', '@type': 'Product'}).replace(
                         'application/ld+json', 'Application/LD+JSON')]:
            with self.subTest(text=text):
                with self.assertRaises(ParityError) as raised:
                    make_baseline(self.compare(text, text), required_jsonld_types={self.required: ['Product']})
                self.assertEqual(raised.exception.code, 'minimum_coverage')
                self.assertEqual(raised.exception.provenance[0]['unmet'][0]['observed_types'], [])

    def test_combined_sorted_diagnostics_and_markdown(self):
        report = self.compare(document(), document())
        with self.assertRaises(ParityError) as raised:
            make_baseline(report, minimum_coverage={'json-ld.entities': 1},
                          minimum_og_properties={'og:title': 1}, minimum_og_nonblank_properties={'og:title': 1},
                          required_jsonld_entities=['z'], required_jsonld_types={'z': ['Z', 'A'], 'a': ['Product']})
        self.assertEqual(raised.exception.provenance[0]['unmet'], [
            {'metric': 'json-ld.entities', 'required': 1, 'observed': 0},
            {'property': 'og:title', 'required': 1, 'observed': 0},
            {'property': 'og:title', 'value_filter': 'nonblank', 'required': 1, 'observed': 0},
            {'entity': 'z', 'required': 1, 'observed': 0},
            {'entity': 'a', 'type': 'Product', 'required': 1, 'observed': 0, 'observed_types': []},
            {'entity': 'z', 'type': 'A', 'required': 1, 'observed': 0, 'observed_types': []},
            {'entity': 'z', 'type': 'Z', 'required': 1, 'observed': 0, 'observed_types': []}])
        code, out, _ = self.run_cli('--save-baseline', str(self.baseline), '--format', 'markdown',
                                  '--require-jsonld-type', self.required, 'Product')
        self.assertEqual(code, 2)
        for detail in ['minimum_coverage', self.required, 'Product', 'observed_types', 'required', 'observed', 'selector']:
            self.assertIn(detail, out)

    def test_invalid_library_and_cli_requirements(self):
        report = self.compare(document(), document())
        invalid = [[], 'id', {1: ['Product']}, {'': ['Product']}, {' ': ['Product']},
                   {'id': []}, {'id': 'Product'}, {'id': ['']}, {'id': [' ']},
                   {'id': [False]}, {'id': [None]}, {'id': [{}]}, {'id': ['Product', 'Product']}]
        for req in invalid:
            with self.subTest(req=req), self.assertRaises(ParityError) as raised:
                make_baseline(report, required_jsonld_types=req)
            self.assertEqual(raised.exception.code, 'invalid_config')
        only = self.compare(document(), document(), syntaxes=('opengraph',))
        with self.assertRaises(ParityError) as raised:
            make_baseline(only, required_jsonld_types={'id': ['Product']})
        self.assertEqual(raised.exception.code, 'invalid_config')
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline))[0], 0)
        before = self.baseline.read_bytes()
        cases = [('--require-jsonld-type', identity, kind) for identity, kind in
                 [('', 'Product'), (' ', 'Product'), ('id', ''), ('id', ' ')]]
        cases += [('--require-jsonld-type', 'id', 'Product', '--require-jsonld-type', 'id', 'Product'),
                  ('--syntax', 'opengraph', '--require-jsonld-type', 'id', 'Product')]
        for args in cases:
            with self.subTest(args=args):
                code, out, _ = self.run_cli('--save-baseline', str(self.baseline), *args)
                self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_config'))
                self.assertEqual(self.baseline.read_bytes(), before)
        for flags in [(), ('--baseline', str(self.baseline))]:
            code, out, _ = self.run_cli(*flags, '--require-jsonld-type', 'id', 'Product')
            self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_config'))
            self.assertEqual(self.baseline.read_bytes(), before)

    def test_historical_coverage_and_no_automatic_type_floor(self):
        good = document({'@id': '../product', '@type': 'Product'})
        wrong = document({'@id': '../product', '@type': 'Thing'})
        report = self.compare(good, wrong)
        legacy = make_baseline(report)
        self.assertNotIn('required_jsonld_types', legacy)
        self.assertEqual(make_baseline(report, required_jsonld_types={}), legacy)
        for side in ('old', 'new'):
            del legacy['captures'][side]['coverage']['json-ld']['entity_types']
        for req in [None, {}]:
            saved = copy.deepcopy(legacy)
            if req is not None:
                saved['required_jsonld_types'] = req
            self.baseline.write_text(json.dumps(saved))
            code, out, _ = self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)['accepted_change_count'], 1)
        historical_report = copy.deepcopy(report)
        historical_report['captures'] = legacy['captures']
        with self.assertRaises(ParityError) as raised:
            make_baseline(historical_report, required_jsonld_types={self.required: ['Product']})
        self.assertEqual(raised.exception.code, 'invalid_config')
        saved = copy.deepcopy(legacy)
        saved['required_jsonld_types'] = {self.required: ['Product']}
        self.baseline.write_text(json.dumps(saved))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))
        # New metadata alone also adds no automatic type floor.
        self.baseline.write_text(json.dumps(make_baseline(report)))
        self.assertEqual(self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')[0], 0)

    def test_invalid_saved_requirements_and_type_coverage(self):
        text = document({'@id': '../product', '@type': 'Product'})
        valid = make_baseline(self.compare(text, text))
        for req in [None, [], {'': ['Product']}, {'id': []}, {'id': [False]},
                    {self.required: ['Product', 'Product']}, {self.required: ['Thing']}]:
            with self.subTest(req=req):
                saved = copy.deepcopy(valid)
                saved['required_jsonld_types'] = req
                self.baseline.write_text(json.dumps(saved))
                code, out, _ = self.run_cli('--baseline', str(self.baseline))
                self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))
        for types in [None, [], {}, {'other': ['Product']}, {self.required: 'Product'},
                      {self.required: [False]}, {self.required: [{}]}, {self.required: ['']},
                      {self.required: ['Product', 'Product']}]:
            with self.subTest(types=types):
                saved = copy.deepcopy(valid)
                saved['captures']['old']['coverage']['json-ld']['entity_types'] = types
                self.baseline.write_text(json.dumps(saved))
                code, out, _ = self.run_cli('--baseline', str(self.baseline))
                self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))
        saved = make_baseline(self.compare(document(), document(), syntaxes=('opengraph',)))
        saved['required_jsonld_types'] = {'id': ['Product']}
        self.baseline.write_text(json.dumps(saved))
        code, out, _ = self.run_cli('--syntax', 'opengraph', '--baseline', str(self.baseline))
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))
