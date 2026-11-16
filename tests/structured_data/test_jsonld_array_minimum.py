"""Opt-in presence of original extracted nonempty top-level JSON-LD arrays."""

import copy
import json

from qawrench.structured_data import ParityError, make_baseline
from . import test_baseline
from .test_parity import FixtureCase, document


class JsonldArrayMinimumTests(FixtureCase):
    run_cli = test_baseline.BaselineTests.run_cli

    def setUp(self):
        super().setUp()
        self.baseline = self.directory / 'accepted.json'
        self.required = 'https://fixture.test/product'
        self.value = {'@id': '../product', '@type': 'Product', 'name': 'Mug'}

    def test_named_product_with_name_but_no_image_cannot_satisfy_requirement(self):
        text = document(self.value)
        report = self.compare(text, text, syntaxes=('json-ld',))
        make_baseline(report, required_jsonld_entities=[self.required],
                      required_jsonld_types={self.required: ['Product']},
                      required_jsonld_nonblank_properties={self.required: ['name']})
        code, out, err = self.run_cli('--syntax', 'json-ld', '--save-baseline', str(self.baseline),
                                     '--require-jsonld-nonempty-array', self.required, 'image')
        self.assertEqual(code, 2)
        self.assertTrue(out, err)
        error = json.loads(out)['errors'][0]
        self.assertEqual(error['code'], 'minimum_coverage')
        self.assertFalse(self.baseline.exists())
        self.assertEqual([p['capture'] for p in error['provenance']], ['old', 'new'])
        for source in error['provenance']:
            self.assertEqual(source['unmet'], [{'entity': self.required, 'property': 'image',
                                               'value_filter': 'nonempty-array', 'required': 1,
                                               'observed': 0, 'observed_type': 'missing'}])
            self.assertIn('selector', source['coverage']['json-ld'])

    def test_original_lists_presence_only_before_set_and_normalization(self):
        for value in [None, '', 'image.jpg', 0, 1.5, False, {}, {'@list': ['image.jpg']}, []]:
            with self.subTest(value=value):
                text = document(dict(self.value, image=value))
                with self.assertRaises(ParityError) as raised:
                    make_baseline(self.compare(text, text), required_jsonld_nonempty_arrays={self.required: ['image']})
                self.assertEqual(raised.exception.code, 'minimum_coverage')
                unmet = raised.exception.provenance[0]['unmet'][0]
                self.assertEqual(unmet['observed'], 0)
                if value == []:
                    self.assertEqual((unmet['observed_type'], unmet['observed_length']), ('array', 0))
        for value in [[None], [''], [{}], [[]], ['image.jpg', 'image.jpg']]:
            with self.subTest(value=value):
                text = document(dict(self.value, image=value))
                report = self.compare(text, text, set_valued=('/image',))
                make_baseline(report, required_jsonld_nonempty_arrays={self.required: ['image']})
                self.assertEqual(report['captures']['old']['coverage']['json-ld']['entity_property_shapes'][self.required]['image'],
                                 {'type': 'array', 'length': len(value)})
        report = self.compare(document(dict(self.value, image=[' A ', 'A'])),
                              document(dict(self.value, image=['A'])),
                              set_valued=('/image',), normalize={'/image/*': 'trim'})
        self.assertEqual(report['changes'], [])
        saved = make_baseline(report, required_jsonld_nonempty_arrays={self.required: ['image']})
        self.assertEqual(saved['captures']['old']['coverage']['json-ld']['entity_property_shapes'][self.required]['image']['length'], 2)
        self.assertEqual(saved['captures']['new']['coverage']['json-ld']['entity_property_shapes'][self.required]['image']['length'], 1)

    def test_save_replay_combined_guards_and_acceptance_cannot_hide_array_loss(self):
        left = document(dict(self.value, image=['one'], sameAs=[None], description='Cup'),
                        meta='<meta property="og:title" content="Mug">')
        right = left.replace('Cup', 'Glass')
        self.compare(left, right)
        flags = ('--require-jsonld-nonempty-array', self.required, 'sameAs',
                 '--require-jsonld-nonempty-array', self.required, 'image',
                 '--require-jsonld-nonblank-property', self.required, 'name',
                 '--require-jsonld-type', self.required, 'Product', '--require-jsonld-entity', self.required,
                 '--minimum-coverage', 'json-ld.entities', '1', '--minimum-og-property', 'og:title', '1',
                 '--minimum-og-nonblank-property', 'og:title', '1')
        code, out, _ = self.run_cli('--save-baseline', str(self.baseline), '--fail-on-diff', *flags)
        self.assertEqual(code, 1)
        saved = json.loads(self.baseline.read_text())
        self.assertEqual(saved['required_jsonld_nonempty_arrays'], {self.required: ['image', 'sameAs']})
        self.assertNotIn('required_jsonld_nonempty_arrays', saved['policy'])
        self.assertEqual(json.loads(out)['changes'][0]['old'], 'Cup')
        self.assertEqual(self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')[0], 0)
        # Even an explicitly accepted future removal must not bypass presence.
        saved['accepted_changes'].append({'syntax': 'json-ld', 'scope': 'entity', 'entity': self.required,
                                         'kind': 'removed', 'path': '/image/0', 'old': 'one'})
        self.baseline.write_text(json.dumps(saved))
        before = self.baseline.read_bytes()
        self.new.write_text(right.replace('["one"]', '[]'))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        error = json.loads(out)
        self.assertEqual((code, error['errors'][0]['code'], error['changes']), (2, 'minimum_coverage', []))
        self.assertEqual([p['capture'] for p in error['errors'][0]['provenance']], ['new'])
        self.assertEqual(self.baseline.read_bytes(), before)

    def test_both_initial_sides_json_markdown_and_baseline_preservation(self):
        good = document(dict(self.value, image=[None]))
        self.compare(good, good)
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline))[0], 0)
        before = self.baseline.read_bytes()
        for value in [None, [], 'image.jpg', {}]:
            bad = document(dict(self.value, image=value))
            for left, right, side, fmt in [(bad, good, 'old', 'json'), (good, bad, 'new', 'markdown')]:
                with self.subTest(value=value, side=side):
                    self.compare(left, right)
                    output = self.directory / ('error.' + fmt)
                    code, _, _ = self.run_cli('--save-baseline', str(self.baseline), '--out', str(output),
                                              '--format', fmt, '--require-jsonld-nonempty-array', self.required, 'image')
                    self.assertEqual(code, 2)
                    content = output.read_text()
                    for detail in ['minimum_coverage', self.required, 'image', 'nonempty-array', 'required', 'observed_type', 'selector']:
                        self.assertIn(detail, content)
                    if fmt == 'json':
                        error = json.loads(content)
                        self.assertEqual((error['status'], error['changes']), ('error', []))
                        self.assertEqual([p['capture'] for p in error['errors'][0]['provenance']], [side])
                    self.assertEqual(self.baseline.read_bytes(), before)

    def test_exact_identity_and_top_level_literal_names(self):
        for value in [dict(self.value, Image=['one'], nested={'image': ['one']}),
                      dict(self.value, **{'@context': {'picture': 'image'}, 'picture': ['one']}),
                      {'@id': '../other', 'image': ['one']}, {'@graph': []}]:
            with self.subTest(value=value), self.assertRaises(ParityError) as raised:
                make_baseline(self.compare(document(value), document(value)),
                              required_jsonld_nonempty_arrays={self.required: ['image']})
            self.assertEqual(raised.exception.code, 'minimum_coverage')
        text = document(dict(self.value, image=['one'])).replace('application/ld+json', 'Application/LD+JSON')
        with self.assertRaises(ParityError) as raised:
            make_baseline(self.compare(text, text), required_jsonld_nonempty_arrays={self.required: ['image']})
        self.assertEqual(raised.exception.code, 'minimum_coverage')
        text = document({'@graph': [dict(self.value, **{' /image~* ': [None]}), {'@id': '_:node', 'image': [[]]}]})
        report = self.compare(text, text)
        req = {self.required: [' /image~* '], '_:node': ['image']}
        saved = make_baseline(report, required_jsonld_nonempty_arrays=req)
        req[self.required].append('missing')
        self.assertEqual(saved['required_jsonld_nonempty_arrays'][self.required], [' /image~* '])
        self.baseline.write_text(json.dumps(saved))
        self.assertEqual(self.run_cli('--baseline', str(self.baseline))[0], 0)
        with self.assertRaises(ParityError):
            make_baseline(report, required_jsonld_nonempty_arrays={'../product': [' /image~* ']})
        text = document({'sku': ' SKU 12 ', 'image': [None]})
        report = self.compare(text, text, identity_field='sku')
        self.baseline.write_text(json.dumps(make_baseline(report, required_jsonld_nonempty_arrays={' SKU 12 ': ['image']})))
        self.assertEqual(self.run_cli('--identity-field', 'sku', '--baseline', str(self.baseline))[0], 0)
        with self.assertRaises(ParityError):
            make_baseline(report, required_jsonld_nonempty_arrays={'SKU 12': ['image']})

    def test_invalid_library_cli_requirements_and_modes(self):
        report = self.compare(document(), document())
        invalid = [[], 'id', {1: ['image']}, {'': ['image']}, {' ': ['image']}, {'id': []},
                   {'id': 'image'}, {'id': ['']}, {'id': [' ']}, {'id': [False]},
                   {'id': [None]}, {'id': [{}]}, {'id': ['image', 'image']}]
        for req in invalid:
            with self.subTest(req=req), self.assertRaises(ParityError) as raised:
                make_baseline(report, required_jsonld_nonempty_arrays=req)
            self.assertEqual(raised.exception.code, 'invalid_config')
        only = self.compare(document(), document(), syntaxes=('opengraph',))
        with self.assertRaises(ParityError) as raised:
            make_baseline(only, required_jsonld_nonempty_arrays={'id': ['image']})
        self.assertEqual(raised.exception.code, 'invalid_config')
        self.assertNotIn('required_jsonld_nonempty_arrays', make_baseline(only, required_jsonld_nonempty_arrays={}))
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline))[0], 0)
        before = self.baseline.read_bytes()
        cases = [('--require-jsonld-nonempty-array', identity, name) for identity, name in
                 [('', 'image'), (' ', 'image'), ('id', ''), ('id', ' ')]]
        cases += [('--require-jsonld-nonempty-array', 'id', 'image', '--require-jsonld-nonempty-array', 'id', 'image'),
                  ('--syntax', 'opengraph', '--require-jsonld-nonempty-array', 'id', 'image')]
        for args in cases:
            with self.subTest(args=args):
                code, out, _ = self.run_cli('--save-baseline', str(self.baseline), *args)
                self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_config'))
                self.assertEqual(self.baseline.read_bytes(), before)
        for flags in [(), ('--baseline', str(self.baseline))]:
            code, out, _ = self.run_cli(*flags, '--require-jsonld-nonempty-array', 'id', 'image')
            self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_config'))

    def test_historical_baselines_and_new_metadata_add_no_implicit_floor(self):
        good = document(dict(self.value, image=['one']))
        bad = document(dict(self.value, image=[]))
        # Arrays may disappear as reviewed field deltas unless explicitly required.
        report = self.compare(good, bad)
        saved = make_baseline(report)
        self.baseline.write_text(json.dumps(saved))
        self.assertEqual(self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')[0], 0)
        historical = copy.deepcopy(report)
        for side in ('old', 'new'):
            del historical['captures'][side]['coverage']['json-ld']['entity_property_shapes']
            del historical['captures'][side]['coverage']['json-ld']['entity_array_nonblank_string_counts']
            del historical['captures'][side]['coverage']['json-ld']['entity_array_distinct_nonblank_string_counts']
            del historical['captures'][side]['coverage']['json-ld']['entity_array_nonblank_string_members']
        saved = make_baseline(historical)
        self.baseline.write_text(json.dumps(saved))
        self.assertEqual(self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')[0], 0)
        with self.assertRaises(ParityError) as raised:
            make_baseline(historical, required_jsonld_nonempty_arrays={self.required: ['image']})
        self.assertEqual(raised.exception.code, 'invalid_config')
        saved['required_jsonld_nonempty_arrays'] = {self.required: ['image']}
        self.baseline.write_text(json.dumps(saved))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))

    def test_saved_requirements_shapes_and_shortfalls_cannot_bypass_guard(self):
        text = document(dict(self.value, image=[None]))
        valid = make_baseline(self.compare(text, text), required_jsonld_nonempty_arrays={self.required: ['image']})
        for req in [None, [], {'': ['image']}, {'id': []}, {'id': [False]},
                    {self.required: ['image', 'image']}, {self.required: ['missing']}]:
            with self.subTest(req=req):
                saved = copy.deepcopy(valid)
                saved['required_jsonld_nonempty_arrays'] = req
                self.baseline.write_text(json.dumps(saved))
                code, out, _ = self.run_cli('--baseline', str(self.baseline))
                self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))
        invalid = [None, [], {}, {'other': {}}, {self.required: []}]
        invalid += [{self.required: {'image': shape}} for shape in [None, [], {}, {'type': []},
                    {'type': 'missing'}, {'type': 'array'}, {'type': 'array', 'length': True},
                    {'type': 'array', 'length': -1}, {'type': 'array', 'length': 1.0},
                    {'type': 'array', 'length': '1'}, {'type': 'array', 'length': 0},
                    {'type': 'string', 'length': 1}, {'type': 'string'},
                    {'type': 'array', 'length': 1, 'extra': 1}]]
        for shapes in invalid:
            with self.subTest(shapes=shapes):
                saved = copy.deepcopy(valid)
                saved['captures']['new']['coverage']['json-ld']['entity_property_shapes'] = shapes
                self.baseline.write_text(json.dumps(saved))
                code, out, _ = self.run_cli('--baseline', str(self.baseline))
                self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))
        saved = make_baseline(self.compare(document(), document(), syntaxes=('opengraph',)))
        saved['required_jsonld_nonempty_arrays'] = {'id': ['image']}
        self.baseline.write_text(json.dumps(saved))
        code, out, _ = self.run_cli('--syntax', 'opengraph', '--baseline', str(self.baseline))
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))
