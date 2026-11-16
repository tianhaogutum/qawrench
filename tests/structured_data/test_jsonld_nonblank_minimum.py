"""Named JSON-LD fields require original extracted nonblank strings, opt-in."""

import copy
import json

from qawrench.structured_data import ParityError, make_baseline
from . import test_baseline
from .test_parity import FixtureCase, document


class JsonldNonblankMinimumTests(FixtureCase):
    run_cli = test_baseline.BaselineTests.run_cli

    def setUp(self):
        super().setUp()
        self.baseline = self.directory / 'accepted.json'
        self.required = 'https://fixture.test/product'

    def test_identity_and_type_without_name_cannot_satisfy_requirement(self):
        text = document({'@id': '../product', '@type': 'Product'})
        report = self.compare(text, text, syntaxes=('json-ld',))
        make_baseline(report, required_jsonld_entities=[self.required],
                      required_jsonld_types={self.required: ['Product']})
        code, out, err = self.run_cli('--syntax', 'json-ld', '--save-baseline', str(self.baseline),
                                     '--require-jsonld-nonblank-property', self.required, 'name')
        self.assertEqual(code, 2)
        self.assertTrue(out, err)
        error = json.loads(out)['errors'][0]
        self.assertEqual(error['code'], 'minimum_coverage')
        self.assertFalse(self.baseline.exists())
        self.assertEqual([p['capture'] for p in error['provenance']], ['old', 'new'])
        for source in error['provenance']:
            self.assertEqual(source['unmet'], [{'entity': self.required, 'property': 'name',
                                               'value_filter': 'nonblank', 'required': 1, 'observed': 0}])
            self.assertIn('selector', source['coverage']['json-ld'])

    def test_save_replay_combined_requirements_and_name_loss(self):
        value = {'@id': '../product', '@type': 'Product', 'name': '\u2003 Mug\u00a0', 'description': 'Cup'}
        text = document(value, meta='<meta property="og:title" content="Mug">')
        self.compare(text, text.replace('Cup', 'Glass'))
        flags = ('--require-jsonld-nonblank-property', self.required, 'name',
                 '--require-jsonld-nonblank-property', self.required, 'description',
                 '--require-jsonld-type', self.required, 'Product', '--require-jsonld-entity', self.required,
                 '--minimum-coverage', 'json-ld.entities', '1', '--minimum-og-property', 'og:title', '1',
                 '--minimum-og-nonblank-property', 'og:title', '1')
        code, out, _ = self.run_cli('--save-baseline', str(self.baseline), '--fail-on-diff', *flags)
        self.assertEqual(code, 1)
        saved = json.loads(self.baseline.read_text())
        self.assertEqual(saved['required_jsonld_nonblank_properties'], {self.required: ['description', 'name']})
        self.assertNotIn('required_jsonld_nonblank_properties', saved['policy'])
        self.assertEqual(json.loads(out)['changes'][0]['old'], 'Cup')
        self.assertEqual(self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')[0], 0)
        before = self.baseline.read_bytes()
        value['name'] = ' \t\u00a0\u2003\n'
        self.new.write_text(document(value, meta='<meta property="og:title" content="Mug">'))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'minimum_coverage'))
        self.assertEqual(json.loads(out)['changes'], [])
        self.assertEqual(json.loads(out)['errors'][0]['provenance'][0]['capture'], 'new')
        self.assertEqual(self.baseline.read_bytes(), before)

    def test_each_initial_capture_preserves_baseline_and_json_markdown_errors(self):
        good = document({'@id': '../product', '@type': 'Product', 'name': 'Mug'})
        blank = document({'@id': '../product', '@type': 'Product', 'name': ''})
        self.compare(good, good)
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline))[0], 0)
        before = self.baseline.read_bytes()
        for left, right, failed, fmt in [(blank, good, 'old', 'json'), (good, blank, 'new', 'markdown')]:
            with self.subTest(side=failed):
                self.compare(left, right)
                output = self.directory / ('error.' + fmt)
                code, _, _ = self.run_cli('--save-baseline', str(self.baseline), '--out', str(output),
                                          '--format', fmt, '--require-jsonld-nonblank-property', self.required, 'name')
                self.assertEqual(code, 2)
                content = output.read_text()
                for detail in ['minimum_coverage', self.required, 'name', 'nonblank', 'required', 'observed', 'selector']:
                    self.assertIn(detail, content)
                if fmt == 'json':
                    error = json.loads(content)
                    self.assertEqual((error['status'], error['changes']), ('error', []))
                    self.assertEqual([p['capture'] for p in error['errors'][0]['provenance']], [failed])
                self.assertEqual(self.baseline.read_bytes(), before)

    def test_nonblank_uses_only_original_top_level_strings_without_coercion(self):
        for value in [None, '', ' ', '\t\n\u00a0\u2003', 0, 12, False, {}, {'@value': 'Mug'},
                      ['Mug'], [''], [['Mug']]]:
            with self.subTest(value=value):
                text = document({'@id': '../product', 'name': value})
                report = self.compare(text, text)
                with self.assertRaises(ParityError) as raised:
                    make_baseline(report, required_jsonld_nonblank_properties={self.required: ['name']})
                self.assertEqual(raised.exception.code, 'minimum_coverage')
                self.assertNotIn('name', report['captures']['old']['coverage']['json-ld']['entity_nonblank_properties'][self.required])
        for value in ['\u2003Mug\u00a0', '\u200b', '0']:
            with self.subTest(value=value):
                report = self.compare(document({'@id': '../product', 'name': value}),
                                      document({'@id': '../product', 'name': 'Changed'}),
                                      normalize={'/name': 'trim'})
                make_baseline(report, required_jsonld_nonblank_properties={self.required: ['name']})
                self.assertEqual(report['changes'][0]['old'], value.strip())
        # Normalized deltas can be accepted, but raw presence is captured separately.
        report = self.compare(document({'@id': '../product', 'name': '\u2003Mug\u00a0'}),
                              document({'@id': '../product', 'name': 'Mug'}), normalize={'/name': 'trim'})
        self.assertEqual(report['changes'], [])
        make_baseline(report, required_jsonld_nonblank_properties={self.required: ['name']})
        blank = document({'@id': '../product', 'name': '\u00a0\u2003'})
        with self.assertRaises(ParityError) as raised:
            make_baseline(self.compare(blank, blank, normalize={'/name': 'collapse-whitespace'}),
                          required_jsonld_nonblank_properties={self.required: ['name']})
        self.assertEqual(raised.exception.code, 'minimum_coverage')

    def test_exact_field_and_identity_matching_no_recursive_or_alias_search(self):
        for value in [
            {'@id': '../other', 'name': 'Mug'},
            {'@id': '../product', 'Name': 'Mug', 'nested': {'name': 'Mug'}},
            {'@id': '../product', '@context': {'label': 'name'}, 'label': 'Mug'},
            {'@graph': []},
        ]:
            with self.subTest(value=value), self.assertRaises(ParityError) as raised:
                make_baseline(self.compare(document(value), document(value)),
                              required_jsonld_nonblank_properties={self.required: ['name']})
            self.assertEqual(raised.exception.code, 'minimum_coverage')
        text = document({'@id': '../product', 'name': 'Mug'}).replace('application/ld+json', 'Application/LD+JSON')
        with self.assertRaises(ParityError):
            make_baseline(self.compare(text, text), required_jsonld_nonblank_properties={self.required: ['name']})
        # These are literal field names, never JSON pointers or wildcard policies.
        text = document({'@graph': [{'@id': '../product', ' /name~* ': 'Mug', '': 'Empty key', ' ': 'Space key'},
                                   {'@id': '_:node', 'name': 'Other'}]})
        report = self.compare(text, text)
        req = {self.required: [' /name~* ', '@id'], '_:node': ['name']}
        saved = make_baseline(report, required_jsonld_nonblank_properties=req)
        req[self.required].append('missing')
        self.assertEqual(saved['required_jsonld_nonblank_properties'][self.required], [' /name~* ', '@id'])
        self.baseline.write_text(json.dumps(saved))
        self.assertEqual(self.run_cli('--baseline', str(self.baseline))[0], 0)
        with self.assertRaises(ParityError):
            make_baseline(report, required_jsonld_nonblank_properties={'../product': [' /name~* ']})
        text = document({'sku': ' SKU 12 ', 'name': 'Mug'})
        report = self.compare(text, text, identity_field='sku')
        saved = make_baseline(report, required_jsonld_nonblank_properties={' SKU 12 ': ['name']})
        self.baseline.write_text(json.dumps(saved))
        self.assertEqual(self.run_cli('--identity-field', 'sku', '--baseline', str(self.baseline))[0], 0)
        with self.assertRaises(ParityError):
            make_baseline(report, required_jsonld_nonblank_properties={'SKU 12': ['name']})

    def test_invalid_library_cli_and_flag_modes(self):
        report = self.compare(document(), document())
        invalid = [[], 'id', {1: ['name']}, {'': ['name']}, {' ': ['name']}, {'id': []},
                   {'id': 'name'}, {'id': ['']}, {'id': [' ']}, {'id': [False]},
                   {'id': [None]}, {'id': [{}]}, {'id': ['name', 'name']}]
        for req in invalid:
            with self.subTest(req=req), self.assertRaises(ParityError) as raised:
                make_baseline(report, required_jsonld_nonblank_properties=req)
            self.assertEqual(raised.exception.code, 'invalid_config')
        only = self.compare(document(), document(), syntaxes=('opengraph',))
        with self.assertRaises(ParityError) as raised:
            make_baseline(only, required_jsonld_nonblank_properties={'id': ['name']})
        self.assertEqual(raised.exception.code, 'invalid_config')
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline))[0], 0)
        before = self.baseline.read_bytes()
        cases = [('--require-jsonld-nonblank-property', identity, name) for identity, name in
                 [('', 'name'), (' ', 'name'), ('id', ''), ('id', ' ')]]
        cases += [('--require-jsonld-nonblank-property', 'id', 'name', '--require-jsonld-nonblank-property', 'id', 'name'),
                  ('--syntax', 'opengraph', '--require-jsonld-nonblank-property', 'id', 'name')]
        for args in cases:
            with self.subTest(args=args):
                code, out, _ = self.run_cli('--save-baseline', str(self.baseline), *args)
                self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_config'))
                self.assertEqual(self.baseline.read_bytes(), before)
        for flags in [(), ('--baseline', str(self.baseline))]:
            code, out, _ = self.run_cli(*flags, '--require-jsonld-nonblank-property', 'id', 'name')
            self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_config'))
            self.assertEqual(self.baseline.read_bytes(), before)

    def test_historical_coverage_and_no_automatic_nonblank_loss_floor(self):
        good = document({'@id': '../product', 'name': 'Mug'})
        blank = document({'@id': '../product', 'name': ''})
        report = self.compare(good, blank)
        saved = make_baseline(report)
        self.assertNotIn('required_jsonld_nonblank_properties', saved)
        self.assertEqual(make_baseline(report, required_jsonld_nonblank_properties={}), saved)
        self.baseline.write_text(json.dumps(saved))
        self.assertEqual(self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')[0], 0)
        for side in ('old', 'new'):
            del saved['captures'][side]['coverage']['json-ld']['entity_nonblank_properties']
        for req in [None, {}]:
            legacy = copy.deepcopy(saved)
            if req is not None:
                legacy['required_jsonld_nonblank_properties'] = req
            self.baseline.write_text(json.dumps(legacy))
            self.assertEqual(self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')[0], 0)
        historical_report = copy.deepcopy(report)
        historical_report['captures'] = saved['captures']
        with self.assertRaises(ParityError) as raised:
            make_baseline(historical_report, required_jsonld_nonblank_properties={self.required: ['name']})
        self.assertEqual(raised.exception.code, 'invalid_config')
        saved['required_jsonld_nonblank_properties'] = {self.required: ['name']}
        self.baseline.write_text(json.dumps(saved))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))

    def test_saved_schema_and_saved_shortfalls_cannot_bypass_guard(self):
        text = document({'@id': '../product', 'name': 'Mug'})
        valid = make_baseline(self.compare(text, text))
        for req in [None, [], {'': ['name']}, {'id': []}, {'id': [False]},
                    {self.required: ['name', 'name']}, {self.required: ['missing']}]:
            with self.subTest(req=req):
                saved = copy.deepcopy(valid)
                saved['required_jsonld_nonblank_properties'] = req
                self.baseline.write_text(json.dumps(saved))
                code, out, _ = self.run_cli('--baseline', str(self.baseline))
                self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))
        for properties in [None, [], {}, {'other': ['name']}, {self.required: 'name'},
                           {self.required: [False]}, {self.required: [{}]}, {self.required: ['name', 'name']}]:
            with self.subTest(properties=properties):
                saved = copy.deepcopy(valid)
                saved['captures']['old']['coverage']['json-ld']['entity_nonblank_properties'] = properties
                self.baseline.write_text(json.dumps(saved))
                code, out, _ = self.run_cli('--baseline', str(self.baseline))
                self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))
        saved = make_baseline(self.compare(document(), document(), syntaxes=('opengraph',)))
        saved['required_jsonld_nonblank_properties'] = {'id': ['name']}
        self.baseline.write_text(json.dumps(saved))
        code, out, _ = self.run_cli('--syntax', 'opengraph', '--baseline', str(self.baseline))
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))

    def test_combined_diagnostics_are_sorted_after_existing_requirements(self):
        report = self.compare(document(), document())
        with self.assertRaises(ParityError) as raised:
            make_baseline(report, minimum_coverage={'json-ld.entities': 1},
                          minimum_og_properties={'og:title': 1}, minimum_og_nonblank_properties={'og:title': 1},
                          required_jsonld_entities=['z'], required_jsonld_types={'z': ['Product']},
                          required_jsonld_nonblank_properties={'z': ['name', 'description'], 'a': ['name']})
        self.assertEqual(raised.exception.code, 'minimum_coverage')
        self.assertEqual(raised.exception.provenance[0]['unmet'], [
            {'metric': 'json-ld.entities', 'required': 1, 'observed': 0},
            {'property': 'og:title', 'required': 1, 'observed': 0},
            {'property': 'og:title', 'value_filter': 'nonblank', 'required': 1, 'observed': 0},
            {'entity': 'z', 'required': 1, 'observed': 0},
            {'entity': 'z', 'type': 'Product', 'required': 1, 'observed': 0, 'observed_types': []},
            {'entity': 'a', 'property': 'name', 'value_filter': 'nonblank', 'required': 1, 'observed': 0},
            {'entity': 'z', 'property': 'description', 'value_filter': 'nonblank', 'required': 1, 'observed': 0},
            {'entity': 'z', 'property': 'name', 'value_filter': 'nonblank', 'required': 1, 'observed': 0}])
