"""Explicit ceilings on original immediate nonblank JSON-LD array strings."""
import copy
import json

from qawrench.structured_data import ParityError, make_baseline
from qawrench.structured_data.baseline import apply_baseline
from . import test_baseline
from .test_parity import FixtureCase, document

FIELD = 'entity_array_nonblank_string_counts'
REQUIREMENT = 'maximum_jsonld_array_nonblank_strings'
FLAG = '--maximum-jsonld-array-nonblank-string'


class JsonldArrayMaximumTests(FixtureCase):
    run_cli = test_baseline.BaselineTests.run_cli

    def setUp(self):
        super().setUp()
        self.baseline = self.directory / 'accepted.json'
        self.identity = 'https://fixture.test/product'
        self.value = {'@id': '../product', '@type': 'Product', 'name': 'Mug'}
        self.requirements = {self.identity: {'image': 2}}
        self.members = {self.identity: {'image': ['front.png', 'back.png']}}
        self.flags = (FLAG, self.identity, 'image', '2')

    def save(self, report, requirements=None, **kwargs):
        return make_baseline(report, maximum_jsonld_array_nonblank_strings=(
            self.requirements if requirements is None else requirements), **kwargs)

    def test_allowed_required_strings_still_exceed_maximum(self):
        text = document(dict(self.value, image=['front.png', 'front.png', 'back.png']))
        report = self.compare(text, text)
        make_baseline(report, allowed_jsonld_array_strings=self.members,
                      required_jsonld_array_strings=self.members,
                      minimum_jsonld_array_nonblank_strings=self.requirements)
        code, out, err = self.run_cli('--save-baseline', str(self.baseline), *self.flags)
        self.assertEqual(code, 2)
        self.assertTrue(out, err)
        error = json.loads(out)['errors'][0]
        self.assertEqual(error['code'], 'maximum_coverage')
        self.assertFalse(self.baseline.exists())
        self.assertEqual([p['capture'] for p in error['provenance']], ['old', 'new'])
        for p in error['provenance']:
            self.assertEqual(p['unmet'], [{'entity': self.identity, 'property': 'image',
                'value_filter': 'array-nonblank-string', 'maximum': 2,
                'observed': 3, 'observed_type': 'array'}])
            self.assertIn('selector', p['coverage']['json-ld'])

    def test_zero_positive_mixed_and_independent_minimums(self):
        for values, maximum, observed in [([], 0, 0), (['', ' \t\n\u00a0', None, 1, True, {},
                ['nested'], {'@value': 'nested'}], 0, 0), (['front.png', 'back.png', None, ' '], 2, 2),
                (['x', 'x'], 2, 2), (['\u200b', 'not a URL'], 2, 2)]:
            with self.subTest(values=values):
                text = document(dict(self.value, image=values))
                report = self.compare(text, text)
                req = {self.identity: {'image': maximum}}
                saved = self.save(report, req)
                req[self.identity]['image'] = 999
                self.assertEqual(saved[REQUIREMENT], {self.identity: {'image': maximum}})
                self.assertEqual(saved['captures']['old']['coverage']['json-ld'][FIELD][self.identity]['image'], observed)
                self.assertNotIn(REQUIREMENT, saved['policy'])
                self.baseline.write_text(json.dumps(saved))
                self.assertEqual(self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')[0], 0)
        text = document(dict(self.value, image=[])); report = self.compare(text, text)
        for kwargs in [dict(required_jsonld_array_strings=self.members),
                       dict(required_jsonld_nonempty_arrays={self.identity: ['image']}),
                       dict(minimum_jsonld_array_nonblank_strings={self.identity: {'image': 1}}),
                       dict(minimum_jsonld_array_distinct_nonblank_strings={self.identity: {'image': 1}})]:
            with self.assertRaises(ParityError) as raised: self.save(report, **kwargs)
            self.assertEqual(raised.exception.code, 'minimum_coverage')

    def test_original_multiplicity_before_normalization_and_set_comparison(self):
        for mode, values in [('trim', [' A ', 'A', 'A']), ('casefold', ['A', 'a', 'a']),
                             ('collapse-whitespace', [' A  B ', 'A B', 'A B'])]:
            report = self.compare(document(dict(self.value, image=values)),
                                  document(dict(self.value, image=[values[1]])),
                                  normalize={'/image/*': mode}, set_valued=('/image',))
            self.assertEqual(report['changes'], [])
            self.assertEqual(report['captures']['old']['coverage']['json-ld'][FIELD][self.identity]['image'], 3)
            with self.assertRaises(ParityError) as raised: self.save(report)
            self.assertEqual(raised.exception.code, 'maximum_coverage')
            self.assertEqual([p['capture'] for p in raised.exception.provenance], ['old'])
        text = document(dict(self.value, image=['\u200b', 'é', 'e\u0301']))
        with self.assertRaises(ParityError): self.save(self.compare(text, text))

    def test_exact_named_identity_literal_property_and_selection(self):
        for value in [dict(self.value, Image=[]), dict(self.value, nested={'image': []}),
                      dict(self.value, picture=[], **{'@context': {'picture': 'image'}}),
                      {'@id': '../other', 'image': []}, {'@graph': []}]:
            with self.subTest(value=value), self.assertRaises(ParityError) as raised:
                self.save(self.compare(document(value), document(value)))
            self.assertEqual(raised.exception.code, 'maximum_coverage')
        text = document(dict(self.value, image=[])).replace('application/ld+json', 'Application/LD+JSON')
        with self.assertRaises(ParityError) as raised: self.save(self.compare(text, text))
        self.assertEqual(raised.exception.provenance[0]['unmet'][0]['observed_type'], 'missing')
        text = document({'@graph': [dict(self.value, **{' /image~* ': []}), {'@id': '_:node', 'image': []}]})
        report = self.compare(text, text)
        self.save(report, {self.identity: {' /image~* ': 0}, '_:node': {'image': 0}})
        for identity in ['../product', self.identity.upper()]:
            with self.assertRaises(ParityError): self.save(report, {identity: {' /image~* ': 0}})
        text = document({'sku': ' SKU 12 ', 'image': []})
        report = self.compare(text, text, identity_field='sku')
        self.save(report, {' SKU 12 ': {'image': 0}})
        with self.assertRaises(ParityError): self.save(report, {'SKU 12': {'image': 0}})

    def test_both_initial_sides_nonarrays_json_markdown_and_preservation(self):
        good = document(dict(self.value, image=['front.png', 'back.png']))
        self.compare(good, good)
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline), *self.flags)[0], 0)
        before = self.baseline.read_bytes()
        for value, kind, count in [(None, 'null', 0), ('front.png', 'string', 0), ({}, 'object', 0),
                                  (1, 'number', 0), (False, 'boolean', 0), ('missing', 'missing', 0),
                                  (['front.png', 'front.png', 'back.png'], 'array', 3)]:
            bad = document(self.value if value == 'missing' else dict(self.value, image=value))
            for left, right, side, fmt in [(bad, good, 'old', 'json'), (good, bad, 'new', 'markdown')]:
                with self.subTest(value=value, side=side):
                    self.compare(left, right)
                    code, out, _ = self.run_cli('--save-baseline', str(self.baseline), '--format', fmt, *self.flags)
                    self.assertEqual(code, 2)
                    for detail in ['maximum_coverage', self.identity, 'image', 'maximum', 'observed', 'observed_type', 'selector']:
                        self.assertIn(detail, out)
                    if fmt == 'json':
                        r = json.loads(out)
                        self.assertEqual((r['status'], r['changes']), ('error', []))
                        p = r['errors'][0]['provenance']
                        self.assertEqual([v['capture'] for v in p], [side])
                        self.assertEqual((p[0]['unmet'][0]['observed_type'], p[0]['unmet'][0]['observed']), (kind, count))
                    self.assertEqual(self.baseline.read_bytes(), before)
        for value in [None, '', {}, 1, False, 'missing']:
            text = document(self.value if value == 'missing' else dict(self.value, image=value))
            with self.assertRaises(ParityError) as raised:
                self.save(self.compare(text, text), {self.identity: {'image': 0}})
            self.assertEqual(raised.exception.code, 'maximum_coverage')

    def test_replay_before_acceptance_both_current_sides_and_prior_guards(self):
        good = document(dict(self.value, image=['front.png', 'back.png']))
        report = self.compare(good, good.replace('Mug', 'Cup'))
        saved = self.save(report, allowed_jsonld_array_strings=self.members,
                          required_jsonld_array_strings=self.members,
                          minimum_jsonld_array_nonblank_strings=self.requirements,
                          minimum_jsonld_array_distinct_nonblank_strings=self.requirements,
                          required_jsonld_entities=[self.identity], required_jsonld_types={self.identity: ['Product']},
                          required_jsonld_nonblank_properties={self.identity: ['name']},
                          required_jsonld_nonempty_arrays={self.identity: ['image']})
        # A saved accepted delta cannot override the explicit ceiling.
        saved['accepted_changes'].append(dict(syntax='json-ld', scope='entity', entity=self.identity,
            kind='added', path='/image/2', new='back.png'))
        self.baseline.write_text(json.dumps(saved)); before = self.baseline.read_bytes()
        self.assertEqual(self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')[0], 0)
        self.new.write_text(good.replace('Mug', 'Cup').replace('"back.png"', '"back.png", "back.png"'))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        r = json.loads(out)
        self.assertEqual((code, r['errors'][0]['code'], r['changes']), (2, 'maximum_coverage', []))
        self.assertEqual([p['capture'] for p in r['errors'][0]['provenance']], ['new'])
        self.assertEqual(self.baseline.read_bytes(), before)
        report['captures']['old']['coverage']['json-ld'][FIELD][self.identity]['image'] = 3
        with self.assertRaises(ParityError) as raised: apply_baseline(report, self.baseline)
        self.assertEqual(raised.exception.code, 'maximum_coverage')
        self.assertEqual([p['capture'] for p in raised.exception.provenance], ['old'])
        self.compare(good, good.replace('Mug', 'Cup'))
        code, out, _ = self.run_cli('--baseline', str(self.baseline), '--set-valued', '/image')
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'baseline_mismatch'))
        self.old.write_text(good+' ')
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'baseline_mismatch'))
        # Shrinking strings is permitted without a separate floor; count/shape guards stay independent.
        smaller = self.compare(good, document(dict(self.value, image=[])))
        self.save(smaller)
        with self.assertRaises(ParityError) as raised:
            self.save(smaller, minimum_jsonld_array_nonblank_strings=self.requirements)
        self.assertEqual(raised.exception.code, 'minimum_coverage')

    def test_invalid_library_cli_and_save_only(self):
        report = self.compare(document(), document())
        invalid = [[], 'id', {1: {'image': 2}}, {' ': {'image': 2}}, {'id': {}}, {'id': []},
                   {'id': {'': 2}}, {'id': {' ': 2}}, {'id': {1: 2}}]
        invalid += [{'id': {'image': v}} for v in [None, [], {}, '2', 2.0, True, False, -1]]
        for req in invalid:
            with self.subTest(req=req), self.assertRaises(ParityError) as raised: self.save(report, req)
            self.assertEqual(raised.exception.code, 'invalid_config')
        only = self.compare(document(), document(), syntaxes=('opengraph',))
        with self.assertRaises(ParityError) as raised: self.save(only)
        self.assertEqual(raised.exception.code, 'invalid_config')
        self.assertNotIn(REQUIREMENT, self.save(only, {}))
        self.assertNotIn(REQUIREMENT, make_baseline(only, maximum_jsonld_array_nonblank_strings=None))
        self.compare(document(), document())
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline))[0], 0)
        before = self.baseline.read_bytes()
        flags = [(FLAG, *triple) for triple in [('', 'image', '2'), (' ', 'image', '2'), ('id', '', '2'),
                 ('id', ' ', '2'), ('id', 'image', '-1'), ('id', 'image', '2.0'), ('id', 'image', 'true')]]
        flags += [(*self.flags, *self.flags), (*self.flags, FLAG, self.identity, 'image', '1'),
                  ('--syntax', 'opengraph', *self.flags)]
        for args in flags:
            code, out, _ = self.run_cli('--save-baseline', str(self.baseline), *args)
            self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_config'))
            self.assertEqual(self.baseline.read_bytes(), before)
        for args in [(), ('--baseline', str(self.baseline))]:
            code, out, _ = self.run_cli(*args, *self.flags)
            self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_config'))

    def test_historical_no_inference_no_implicit_bound(self):
        text = document(dict(self.value, image=['x', 'x', 'x']))
        report = self.compare(text, text)
        for missing in [(), (FIELD,), ('entity_property_shapes', FIELD,
                'entity_array_distinct_nonblank_string_counts', 'entity_array_nonblank_string_members')]:
            historical = copy.deepcopy(report)
            for c in historical['captures'].values():
                for field in missing: c['coverage']['json-ld'].pop(field)
            self.baseline.write_text(json.dumps(make_baseline(historical)))
            self.assertEqual(self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')[0], 0)
            if missing:
                with self.assertRaises(ParityError) as raised: self.save(historical, {self.identity: {'image': 0}})
                self.assertEqual(raised.exception.code, 'invalid_config')
                saved = make_baseline(historical); saved[REQUIREMENT] = {self.identity: {'image': 5}}
                self.baseline.write_text(json.dumps(saved))
                code, out, _ = self.run_cli('--baseline', str(self.baseline))
                self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))
        text = document(dict(self.value, image=[])); report = self.compare(text, text)
        for c in report['captures'].values(): c['coverage']['json-ld'].pop(FIELD)
        with self.assertRaises(ParityError) as raised: self.save(report, {self.identity: {'image': 0}})
        self.assertEqual(raised.exception.code, 'invalid_config')

    def test_saved_schema_metadata_and_ceiling_failures(self):
        text = document(dict(self.value, image=['front.png', 'back.png', None]))
        valid = self.save(self.compare(text, text))
        variants = []
        for req in [None, [], {'id': {}}, {'': {'image': 2}}, {'id': {'': 2}}, {'id': {'image': True}},
                    {'id': {'image': -1}}, {'id': {'image': 2.0}}, {self.identity: {'image': 1}},
                    {self.identity: {'absent': 0}}, {self.identity: {'name': 0}}]:
            saved = copy.deepcopy(valid); saved[REQUIREMENT] = req; variants.append(saved)
        for field in [FIELD, 'entity_property_shapes']:
            saved = copy.deepcopy(valid); del saved['captures']['new']['coverage']['json-ld'][field]; variants.append(saved)
        for counts in [None, [], {}, {'other': {}}, {self.identity: []}, {self.identity: {}},
                       {self.identity: {'name': 2}}, *[{self.identity: {'image': v}} for v in
                            [None, '2', 2.0, True, -1, 4, 0, 1]]]:
            saved = copy.deepcopy(valid); saved['captures']['new']['coverage']['json-ld'][FIELD] = counts; variants.append(saved)
        only = make_baseline(self.compare(document(), document(), syntaxes=('opengraph',)))
        only[REQUIREMENT] = self.requirements
        for saved in variants + [only]:
            self.baseline.write_text(json.dumps(saved)); before = self.baseline.read_bytes()
            code, out, _ = self.run_cli('--baseline', str(self.baseline))
            self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))
            self.assertEqual(self.baseline.read_bytes(), before)
        self.baseline.write_text(json.dumps(valid).replace('"maximum_jsonld_array_nonblank_strings":',
            '"maximum_jsonld_array_nonblank_strings": {}, "maximum_jsonld_array_nonblank_strings":', 1))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))
