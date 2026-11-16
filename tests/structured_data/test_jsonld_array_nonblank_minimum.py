"""Original immediate nonblank string list-element floors, opt-in only."""

import copy
import json

from qawrench.structured_data import ParityError, make_baseline
from . import test_baseline
from .test_parity import FixtureCase, document


class JsonldArrayNonblankMinimumTests(FixtureCase):
    run_cli = test_baseline.BaselineTests.run_cli

    def setUp(self):
        super().setUp()
        self.baseline = self.directory / 'accepted.json'
        self.identity = 'https://fixture.test/product'
        self.value = {'@id': '../product', '@type': 'Product', 'name': 'Mug'}
        self.requirements = {self.identity: {'image': 1}}
        self.flags = ('--minimum-jsonld-array-nonblank-string', self.identity, 'image', '1')

    def test_presence_only_arrays_cannot_satisfy_nonblank_string_floor(self):
        for image in [[None], ['', ' ', {}]]:
            with self.subTest(image=image):
                text = document(dict(self.value, image=image))
                report = self.compare(text, text)
                make_baseline(report, required_jsonld_nonempty_arrays={self.identity: ['image']})
                code, out, err = self.run_cli('--save-baseline', str(self.baseline), *self.flags)
                self.assertEqual(code, 2)
                self.assertTrue(out, err)
                error = json.loads(out)['errors'][0]
                self.assertEqual(error['code'], 'minimum_coverage')
                self.assertFalse(self.baseline.exists())
                self.assertEqual([p['capture'] for p in error['provenance']], ['old', 'new'])
                for source in error['provenance']:
                    self.assertEqual(source['unmet'], [{'entity': self.identity, 'property': 'image',
                        'value_filter': 'array-nonblank-string', 'required': 1, 'observed': 0,
                        'observed_type': 'array'}])
                    self.assertIn('selector', source['coverage']['json-ld'])

    def save(self, report, requirements=None):
        return make_baseline(report, minimum_jsonld_array_nonblank_strings=(
            self.requirements if requirements is None else requirements))

    def test_original_immediate_strings_only_and_zero_floor_shape(self):
        cases = [([None, '', ' \t\n\u00a0', {}, [], ['nested'], {'@value': 'text'}, 1, True], 0),
                 ([' A ', 'A', None, {}, ['nested'], '\u200b', 'not a URL'], 4), ([], 0)]
        for value, count in cases:
            with self.subTest(value=value):
                text = document(dict(self.value, image=value))
                report = self.compare(text, text, set_valued=('/image',))
                counts = report['captures']['old']['coverage']['json-ld']['entity_array_nonblank_string_counts']
                self.assertEqual(counts, {self.identity: {'image': count}})
                self.save(report, {self.identity: {'image': count}})
                with self.assertRaises(ParityError) as raised:
                    self.save(report, {self.identity: {'image': count + 1}})
                self.assertEqual(raised.exception.provenance[0]['unmet'][0]['observed'], count)
        for value in [None, '', 'text', {}, {'@list': ['text']}, False, 0, 1.5]:
            text = document(dict(self.value, image=value))
            with self.subTest(value=value), self.assertRaises(ParityError) as raised:
                self.save(self.compare(text, text), {self.identity: {'image': 0}})
            self.assertEqual(raised.exception.code, 'minimum_coverage')
        text = document(self.value)
        with self.assertRaises(ParityError) as raised:
            self.save(self.compare(text, text), {self.identity: {'image': 0}})
        self.assertEqual(raised.exception.provenance[0]['unmet'][0]['observed_type'], 'missing')
        report = self.compare(document(dict(self.value, image=[' A ', 'A'])),
                              document(dict(self.value, image=['A'])),
                              set_valued=('/image',), normalize={'/image/*': 'trim'})
        self.assertEqual(report['changes'], [])
        with self.assertRaises(ParityError) as raised:
            self.save(report, {self.identity: {'image': 2}})
        self.assertEqual([p['capture'] for p in raised.exception.provenance], ['new'])
        self.assertEqual(raised.exception.provenance[0]['unmet'][0]['observed'], 1)

    def test_exact_literal_property_identity_no_recursion_alias_or_selector_expansion(self):
        for value in [dict(self.value, Image=['one'], nested={'image': ['one']}),
                      dict(self.value, picture=['one'], **{'@context': {'picture': 'image'}}),
                      {'@id': '../other', 'image': ['one']}, {'@graph': []}]:
            with self.subTest(value=value), self.assertRaises(ParityError) as raised:
                self.save(self.compare(document(value), document(value)))
            self.assertEqual(raised.exception.code, 'minimum_coverage')
        text = document(dict(self.value, image=['one'])).replace('application/ld+json', 'Application/LD+JSON')
        with self.assertRaises(ParityError):
            self.save(self.compare(text, text))
        text = document({'@graph': [dict(self.value, **{' /image~* ': ['one']}),
                                    {'@id': '_:node', 'image': ['two']}]})
        report = self.compare(text, text)
        req = {self.identity: {' /image~* ': 1}, '_:node': {'image': 1}}
        saved = self.save(report, req)
        req[self.identity][' /image~* '] = 10
        self.assertEqual(saved['minimum_jsonld_array_nonblank_strings'][self.identity][' /image~* '], 1)
        self.baseline.write_text(json.dumps(saved))
        self.assertEqual(self.run_cli('--baseline', str(self.baseline))[0], 0)
        with self.assertRaises(ParityError):
            self.save(report, {'../product': {' /image~* ': 1}})
        text = document({'sku': ' SKU 12 ', 'image': ['one']})
        report = self.compare(text, text, identity_field='sku')
        self.baseline.write_text(json.dumps(self.save(report, {' SKU 12 ': {'image': 1}})))
        self.assertEqual(self.run_cli('--identity-field', 'sku', '--baseline', str(self.baseline))[0], 0)
        with self.assertRaises(ParityError):
            self.save(report, {'SKU 12': {'image': 1}})

    def test_save_replay_combines_guards_and_accepted_delta_cannot_hide_shortfall(self):
        left = document(dict(self.value, image=['one', 'one'], sameAs=['two'], description='Cup'),
                        meta='<meta property="og:title" content="Mug">')
        right = left.replace('Cup', 'Glass')
        self.compare(left, right)
        flags = (*self.flags, '--minimum-jsonld-array-nonblank-string', self.identity, 'sameAs', '1',
                 '--require-jsonld-entity', self.identity, '--require-jsonld-type', self.identity, 'Product',
                 '--require-jsonld-nonblank-property', self.identity, 'name',
                 '--require-jsonld-nonempty-array', self.identity, 'image',
                 '--minimum-coverage', 'json-ld.entities', '1', '--minimum-og-property', 'og:title', '1',
                 '--minimum-og-nonblank-property', 'og:title', '1')
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline), '--fail-on-diff', *flags)[0], 1)
        saved = json.loads(self.baseline.read_text())
        self.assertEqual(saved['minimum_jsonld_array_nonblank_strings'], {self.identity: {'image': 1, 'sameAs': 1}})
        self.assertNotIn('minimum_jsonld_array_nonblank_strings', saved['policy'])
        self.assertEqual(self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')[0], 0)
        saved['accepted_changes'] += [dict(syntax='json-ld', scope='entity', entity=self.identity,
                                          kind='changed', path='/image/0', old='one', new=None),
                                     dict(syntax='json-ld', scope='entity', entity=self.identity,
                                          kind='removed', path='/image/1', old='one')]
        self.baseline.write_text(json.dumps(saved))
        before = self.baseline.read_bytes()
        self.new.write_text(right.replace('["one", "one"]', '[null]'))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        result = json.loads(out)
        self.assertEqual((code, result['errors'][0]['code'], result['changes']), (2, 'minimum_coverage', []))
        self.assertEqual([p['capture'] for p in result['errors'][0]['provenance']], ['new'])
        self.assertEqual(self.baseline.read_bytes(), before)

    def test_both_initial_sides_json_markdown_preserve_bytes(self):
        good = document(dict(self.value, image=['one']))
        self.compare(good, good)
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline), *self.flags)[0], 0)
        before = self.baseline.read_bytes()
        for value in [None, [], [None], ['', ' ', {}]]:
            bad = document(dict(self.value, image=value))
            for left, right, side, fmt in [(bad, good, 'old', 'json'), (good, bad, 'new', 'markdown')]:
                with self.subTest(value=value, side=side):
                    self.compare(left, right)
                    code, out, _ = self.run_cli('--save-baseline', str(self.baseline), '--format', fmt, *self.flags)
                    self.assertEqual(code, 2)
                    for detail in ['minimum_coverage', self.identity, 'image', 'array-nonblank-string',
                                   'required', 'observed_type', 'selector']:
                        self.assertIn(detail, out)
                    if fmt == 'json':
                        error = json.loads(out)
                        self.assertEqual((error['status'], error['changes']), ('error', []))
                        self.assertEqual([p['capture'] for p in error['errors'][0]['provenance']], [side])
                    self.assertEqual(self.baseline.read_bytes(), before)

    def test_invalid_library_cli_requirements_and_modes(self):
        report = self.compare(document(), document())
        invalid = [[], 'id', {1: {'image': 1}}, {'': {'image': 1}}, {' ': {'image': 1}},
                   {'id': {}}, {'id': []}, {'id': {'': 1}}, {'id': {' ': 1}}, {'id': {1: 1}}]
        invalid += [{'id': {'image': v}} for v in [True, False, None, -1, 1.0, '1', [], {}]]
        for req in invalid:
            with self.subTest(req=req), self.assertRaises(ParityError) as raised:
                self.save(report, req)
            self.assertEqual(raised.exception.code, 'invalid_config')
        only = self.compare(document(), document(), syntaxes=('opengraph',))
        with self.assertRaises(ParityError) as raised:
            self.save(only)
        self.assertEqual(raised.exception.code, 'invalid_config')
        self.assertNotIn('minimum_jsonld_array_nonblank_strings', self.save(only, {}))
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline))[0], 0)
        before = self.baseline.read_bytes()
        flag = '--minimum-jsonld-array-nonblank-string'
        cases = [(flag, i, p, c) for i, p, c in [('', 'image', '1'), (' ', 'image', '1'),
                 ('id', '', '1'), ('id', ' ', '1')]+[('id', 'image', c) for c in ['', ' ', '-1', '1.0', 'true', 'False', 'null']]]
        cases += [(*self.flags, *self.flags), ('--syntax', 'opengraph', *self.flags)]
        for args in cases:
            with self.subTest(args=args):
                code, out, _ = self.run_cli('--save-baseline', str(self.baseline), *args)
                self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_config'))
                self.assertEqual(self.baseline.read_bytes(), before)
        for flags in [(), ('--baseline', str(self.baseline))]:
            code, out, _ = self.run_cli(*flags, *self.flags)
            self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_config'))

    def test_historical_compatibility_and_no_implicit_count_floors(self):
        report = self.compare(document(dict(self.value, image=['one', 'one'])),
                              document(dict(self.value, image=[])))
        self.baseline.write_text(json.dumps(make_baseline(report)))
        self.assertEqual(self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')[0], 0)
        historical = copy.deepcopy(report)
        for side in ('old', 'new'):
            historical['captures'][side]['coverage']['json-ld'].pop('entity_array_nonblank_string_counts')
        self.baseline.write_text(json.dumps(make_baseline(historical)))
        self.assertEqual(self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')[0], 0)
        with self.assertRaises(ParityError) as raised:
            self.save(historical, {self.identity: {'image': 0}})
        self.assertEqual(raised.exception.code, 'invalid_config')
        saved = make_baseline(historical)
        saved['minimum_jsonld_array_nonblank_strings'] = {self.identity: {'image': 0}}
        self.baseline.write_text(json.dumps(saved))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))
        # Previous array-presence guard still works without element metadata.
        text = document(dict(self.value, image=[None]))
        report = self.compare(text, text)
        for c in report['captures'].values(): c['coverage']['json-ld'].pop('entity_array_nonblank_string_counts')
        make_baseline(report, required_jsonld_nonempty_arrays={self.identity: ['image']})

    def test_malformed_saved_requirements_counts_and_shortfalls(self):
        text = document(dict(self.value, image=['one']))
        valid = self.save(self.compare(text, text))
        variants = []
        for req in [None, [], {'': {'image': 1}}, {'id': {}}, {'id': {'image': True}},
                    {'id': {'image': -1}}, {self.identity: {'image': 2}}, {self.identity: {'missing': 0}}]:
            saved = copy.deepcopy(valid); saved['minimum_jsonld_array_nonblank_strings'] = req; variants.append(saved)
        for counts in [None, [], {}, {'other': {}}, {self.identity: []}, {self.identity: {}},
                       {self.identity: {'name': 0}}, *[{self.identity: {'image': v}}
                       for v in [True, False, -1, 1.0, '1', None, 0, 2]]]:
            saved = copy.deepcopy(valid)
            saved['captures']['new']['coverage']['json-ld']['entity_array_nonblank_string_counts'] = counts
            variants.append(saved)
        for field in ['entity_array_nonblank_string_counts', 'entity_property_shapes']:
            saved = copy.deepcopy(valid); del saved['captures']['new']['coverage']['json-ld'][field]; variants.append(saved)
        for shape in [{'type': 'string'}, {'type': 'array', 'length': 0}, {'type': 'array', 'length': True}]:
            saved = copy.deepcopy(valid)
            saved['captures']['new']['coverage']['json-ld']['entity_property_shapes'][self.identity]['image'] = shape
            variants.append(saved)
        for saved in variants:
            with self.subTest(saved=saved):
                self.baseline.write_text(json.dumps(saved)); before = self.baseline.read_bytes()
                code, out, _ = self.run_cli('--baseline', str(self.baseline))
                self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))
                self.assertEqual(self.baseline.read_bytes(), before)
        # Strict saved JSON rejects duplicate identity/property keys too.
        self.baseline.write_text(json.dumps(valid).replace('"image": 1', '"image": 1, "image": 1', 1))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))
        only = make_baseline(self.compare(document(), document(), syntaxes=('opengraph',)))
        only['minimum_jsonld_array_nonblank_strings'] = self.requirements
        self.baseline.write_text(json.dumps(only))
        code, out, _ = self.run_cli('--syntax', 'opengraph', '--baseline', str(self.baseline))
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))
