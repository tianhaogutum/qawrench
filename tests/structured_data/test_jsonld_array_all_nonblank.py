"""Universal constraints on original immediate JSON-LD array elements."""
import copy
import json

from qawrench.structured_data import ParityError, make_baseline
from qawrench.structured_data.baseline import apply_baseline
from . import test_baseline
from .test_parity import FixtureCase, document

FIELD = 'entity_array_nonblank_string_counts'
REQUIREMENT = 'required_jsonld_array_all_nonblank_strings'
FLAG = '--require-jsonld-array-all-nonblank-strings'


class JsonldArrayAllNonblankTests(FixtureCase):
    run_cli = test_baseline.BaselineTests.run_cli

    def setUp(self):
        super().setUp()
        self.baseline = self.directory / 'accepted.json'
        self.identity = 'https://fixture.test/product'
        self.value = {'@id': '../product', '@type': 'Product', 'name': 'Mug'}
        self.requirements = {self.identity: ['image']}
        self.flags = (FLAG, self.identity, 'image')

    def save(self, report, requirements=None, **kwargs):
        return make_baseline(report, required_jsonld_array_all_nonblank_strings=(
            self.requirements if requirements is None else requirements), **kwargs)

    def test_existing_guards_ignore_invalid_elements(self):
        text = document(dict(self.value, image=['front.png', None, {'@value': 'back.png'}, ' ']))
        report = self.compare(text, text)
        members = {self.identity: {'image': ['front.png']}}
        counts = {self.identity: {'image': 1}}
        make_baseline(report, allowed_jsonld_array_strings=members, required_jsonld_array_strings=members,
                      minimum_jsonld_array_nonblank_strings=counts, maximum_jsonld_array_nonblank_strings=counts)
        code, out, err = self.run_cli('--save-baseline', str(self.baseline), *self.flags)
        self.assertEqual(code, 2)
        self.assertTrue(out, err)
        error = json.loads(out)['errors'][0]
        self.assertEqual(error['code'], 'minimum_coverage')
        self.assertFalse(self.baseline.exists())
        self.assertEqual([p['capture'] for p in error['provenance']], ['old', 'new'])
        for p in error['provenance']:
            self.assertEqual(p['unmet'], [{'entity': self.identity, 'property': 'image',
                'value_filter': 'array-all-nonblank-strings', 'required': 'all', 'observed': 1,
                'observed_type': 'array', 'observed_length': 4}])
            self.assertIn('selector', p['coverage']['json-ld'])

    def test_positives_empty_repetition_and_independent_floors(self):
        for values in [[], ['front.png', 'back.png'], ['x', 'x'], ['\u200b', 'not a URL', ' x ', 'é', 'e\u0301']]:
            with self.subTest(values=values):
                text = document(dict(self.value, image=values))
                report = self.compare(text, text)
                req = copy.deepcopy(self.requirements)
                saved = self.save(report, req)
                req[self.identity].append('changed')
                self.assertEqual(saved[REQUIREMENT], self.requirements)
                self.assertNotIn(REQUIREMENT, saved['policy'])
                self.baseline.write_text(json.dumps(saved))
                self.assertEqual(self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')[0], 0)
        for kwargs in [dict(required_jsonld_nonempty_arrays=self.requirements),
                       dict(required_jsonld_array_strings={self.identity: {'image': ['x']}}),
                       dict(minimum_jsonld_array_nonblank_strings={self.identity: {'image': 1}}),
                       dict(minimum_jsonld_array_distinct_nonblank_strings={self.identity: {'image': 1}})]:
            text = document(dict(self.value, image=[]))
            with self.assertRaises(ParityError) as raised: self.save(self.compare(text, text), **kwargs)
            self.assertEqual(raised.exception.code, 'minimum_coverage')
        text = document(dict(self.value, image=['x', 'x']))
        with self.assertRaises(ParityError) as raised:
            self.save(self.compare(text, text), maximum_jsonld_array_nonblank_strings={self.identity: {'image': 1}})
        self.assertEqual(raised.exception.code, 'maximum_coverage')

    def test_each_invalid_element_and_initial_side_preserves_bytes(self):
        good = document(dict(self.value, image=['front.png', 'back.png']))
        self.compare(good, good)
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline), *self.flags)[0], 0)
        before = self.baseline.read_bytes()
        bad_values = [(v, 'array', 1, 2) for v in [[None, 'x'], ['', 'x'], [' \t\n\u00a0', 'x'],
            [False, 'x'], [1, 'x'], [{}, 'x'], [['nested'], 'x'], [{'@value': 'back.png'}, 'x']]]
        bad_values += [(v, kind, 0, None) for v, kind in [(None, 'null'), ('x', 'string'),
            ({}, 'object'), (1, 'number'), (False, 'boolean'), ('missing', 'missing')]]
        for value, kind, count, length in bad_values:
            bad = document(self.value if value == 'missing' else dict(self.value, image=value))
            for left, right, side, fmt in [(bad, good, 'old', 'json'), (good, bad, 'new', 'markdown')]:
                with self.subTest(value=value, side=side):
                    self.compare(left, right)
                    code, out, _ = self.run_cli('--save-baseline', str(self.baseline), '--format', fmt, *self.flags)
                    self.assertEqual(code, 2)
                    for detail in ['minimum_coverage', self.identity, 'image', 'observed_length', 'observed_type', 'selector']:
                        self.assertIn(detail, out)
                    if fmt == 'json':
                        r = json.loads(out); self.assertEqual((r['status'], r['changes']), ('error', []))
                        p = r['errors'][0]['provenance']; self.assertEqual([v['capture'] for v in p], [side])
                        u = p[0]['unmet'][0]
                        self.assertEqual((u['observed_type'], u['observed'], u['observed_length']), (kind, count, length))
                    self.assertEqual(self.baseline.read_bytes(), before)

    def test_original_length_and_multiplicity_before_normalization_and_sets(self):
        for mode, values in [('trim', [' x ', 'x', 'x']), ('casefold', ['X', 'x', 'x']),
                             ('collapse-whitespace', [' x  y ', 'x y', 'x y'])]:
            report = self.compare(document(dict(self.value, image=values)), document(dict(self.value, image=[values[1]])),
                                  normalize={'/image/*': mode}, set_valued=('/image',))
            self.assertEqual(report['changes'], [])
            self.save(report)  # Distinct count 1 is not original qualifying multiplicity 3.
        for values in [[None, 'x'], [' ', 'x'], [{'@value': 'x'}, 'x']]:
            text = document(dict(self.value, image=values))
            with self.assertRaises(ParityError): self.save(self.compare(text, text, set_valued=('/image',)))

    def test_exact_identity_literal_property_and_selector(self):
        for value in [dict(self.value, Image=[]), dict(self.value, nested={'image': []}),
                      dict(self.value, picture=[], **{'@context': {'picture': 'image'}}),
                      {'@id': '../other', 'image': []}, {'@graph': []}]:
            with self.subTest(value=value), self.assertRaises(ParityError) as raised:
                self.save(self.compare(document(value), document(value)))
            self.assertEqual(raised.exception.code, 'minimum_coverage')
        text = document(dict(self.value, image=[])).replace('application/ld+json', 'Application/LD+JSON')
        with self.assertRaises(ParityError) as raised: self.save(self.compare(text, text))
        self.assertEqual(raised.exception.provenance[0]['unmet'][0]['observed_type'], 'missing')
        text = document({'@graph': [dict(self.value, **{' /image~* ': []}), {'@id': '_:node', 'image': []}]})
        report = self.compare(text, text)
        self.save(report, {self.identity: [' /image~* '], '_:node': ['image']})
        for identity in ['../product', self.identity.upper()]:
            with self.assertRaises(ParityError): self.save(report, {identity: [' /image~* ']})
        text = document({'sku': ' SKU 12 ', 'image': []})
        report = self.compare(text, text, identity_field='sku')
        self.save(report, {' SKU 12 ': ['image']})
        with self.assertRaises(ParityError): self.save(report, {'SKU 12': ['image']})

    def test_replay_before_accepted_deltas_and_prior_guards(self):
        good = document(dict(self.value, image=['front.png', 'back.png']))
        report = self.compare(good, good.replace('Mug', 'Cup')); saved = self.save(report)
        saved['accepted_changes'].append(dict(syntax='json-ld', scope='entity', entity=self.identity,
            kind='added', path='/image/2', new=None))
        self.baseline.write_text(json.dumps(saved)); before = self.baseline.read_bytes()
        self.assertEqual(self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')[0], 0)
        self.new.write_text(good.replace('Mug', 'Cup').replace('"back.png"', '"back.png", null'))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        r = json.loads(out)
        self.assertEqual((code, r['errors'][0]['code'], r['changes']), (2, 'minimum_coverage', []))
        self.assertEqual([p['capture'] for p in r['errors'][0]['provenance']], ['new'])
        self.assertEqual(self.baseline.read_bytes(), before)
        report['captures']['old']['coverage']['json-ld']['entity_property_shapes'][self.identity]['image']['length'] = 3
        with self.assertRaises(ParityError) as raised: apply_baseline(report, self.baseline)
        self.assertEqual([p['capture'] for p in raised.exception.provenance], ['old'])
        self.compare(good, good.replace('Mug', 'Cup'))
        code, out, _ = self.run_cli('--baseline', str(self.baseline), '--set-valued', '/image')
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'baseline_mismatch'))
        self.old.write_text(good+' ')
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'baseline_mismatch'))

    def test_invalid_library_cli_and_save_only(self):
        report = self.compare(document(), document())
        invalid = [[], 'id', {1: ['image']}, {' ': ['image']}, {'id': {}}, {'id': []}, {'id': 'image'},
                   {'id': [1]}, {'id': ['']}, {'id': [' ']}, {'id': ['image', 'image']}]
        for req in invalid:
            with self.subTest(req=req), self.assertRaises(ParityError) as raised: self.save(report, req)
            self.assertEqual(raised.exception.code, 'invalid_config')
        only = self.compare(document(), document(), syntaxes=('opengraph',))
        with self.assertRaises(ParityError) as raised: self.save(only)
        self.assertEqual(raised.exception.code, 'invalid_config')
        self.assertNotIn(REQUIREMENT, self.save(only, {}))
        self.assertNotIn(REQUIREMENT, make_baseline(only, required_jsonld_array_all_nonblank_strings=None))
        self.compare(document(), document())
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline))[0], 0)
        before = self.baseline.read_bytes()
        flags = [(FLAG, *pair) for pair in [('', 'image'), (' ', 'image'), ('id', ''), ('id', ' ')]]
        flags += [(*self.flags, *self.flags), ('--syntax', 'opengraph', *self.flags)]
        for args in flags:
            code, out, _ = self.run_cli('--save-baseline', str(self.baseline), *args)
            self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_config'))
            self.assertEqual(self.baseline.read_bytes(), before)
        for args in [(), ('--baseline', str(self.baseline))]:
            code, out, _ = self.run_cli(*args, *self.flags)
            self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_config'))

    def test_shared_policy_messages_keep_existing_nonempty_wording(self):
        # Round-17 drift check: the shared helper must not reword the existing option's errors.
        report = self.compare(document(), document(), syntaxes=('opengraph',))
        with self.assertRaises(ParityError) as raised:
            make_baseline(report, required_jsonld_nonempty_arrays={self.identity: ['image']})
        self.assertEqual(str(raised.exception),
                         'Nonempty JSON-LD array requirements need selected json-ld syntax.')
        json_report = self.compare(document(), document())
        for bad in [{' ': ['image']}, {self.identity: ['']}]:
            with self.subTest(bad=bad), self.assertRaises(ParityError) as raised:
                make_baseline(json_report, required_jsonld_nonempty_arrays=bad)
            self.assertEqual(str(raised.exception), 'Nonempty JSON-LD array requirements need exact nonblank '
                             'identity/property strings and nonempty lists without duplicates.')
        with self.assertRaises(ParityError) as raised:
            make_baseline(report, required_jsonld_array_all_nonblank_strings=self.requirements)
        self.assertEqual(str(raised.exception),
                         'All-elements JSON-LD array requirements need selected json-ld syntax.')

    def test_historical_no_inference_or_implicit_requirement(self):
        text = document(dict(self.value, image=['x', None]))
        report = self.compare(text, text)
        for missing in [(), (FIELD,), ('entity_property_shapes', FIELD,
                'entity_array_distinct_nonblank_string_counts', 'entity_array_nonblank_string_members')]:
            historical = copy.deepcopy(report)
            for c in historical['captures'].values():
                for field in missing: c['coverage']['json-ld'].pop(field)
            self.baseline.write_text(json.dumps(make_baseline(historical)))
            self.assertEqual(self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')[0], 0)
            if missing:
                with self.assertRaises(ParityError) as raised: self.save(historical)
                self.assertEqual(raised.exception.code, 'invalid_config')
                saved = make_baseline(historical); saved[REQUIREMENT] = self.requirements
                self.baseline.write_text(json.dumps(saved))
                code, out, _ = self.run_cli('--baseline', str(self.baseline))
                self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))
        text = document(dict(self.value, image=[])); report = self.compare(text, text)
        for c in report['captures'].values(): c['coverage']['json-ld'].pop(FIELD)
        with self.assertRaises(ParityError) as raised: self.save(report)
        self.assertEqual(raised.exception.code, 'invalid_config')

    def test_saved_schema_and_metadata_validation(self):
        text = document(dict(self.value, image=['x', 'x']))
        valid = self.save(self.compare(text, text)); variants = []
        for req in [None, [], {'id': []}, {'': ['image']}, {'id': ['']}, {'id': ['image','image']},
                    {self.identity: ['absent']}, {self.identity: ['name']}]:
            saved = copy.deepcopy(valid); saved[REQUIREMENT] = req; variants.append(saved)
        for field in [FIELD, 'entity_property_shapes']:
            saved = copy.deepcopy(valid); del saved['captures']['new']['coverage']['json-ld'][field]; variants.append(saved)
        for count in [None, '2', 2.0, True, -1, 3, 0, 1]:
            saved = copy.deepcopy(valid); saved['captures']['new']['coverage']['json-ld'][FIELD][self.identity]['image'] = count
            variants.append(saved)
        saved = copy.deepcopy(valid)
        saved['captures']['new']['coverage']['json-ld']['entity_property_shapes'][self.identity]['image']['length'] = 3
        variants.append(saved)  # Internally valid counts, but universal constraint fails.
        only = make_baseline(self.compare(document(), document(), syntaxes=('opengraph',)))
        only[REQUIREMENT] = self.requirements
        for saved in variants + [only]:
            self.baseline.write_text(json.dumps(saved)); before = self.baseline.read_bytes()
            code, out, _ = self.run_cli('--baseline', str(self.baseline))
            self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))
            self.assertEqual(self.baseline.read_bytes(), before)
        self.baseline.write_text(json.dumps(valid).replace('"'+REQUIREMENT+'":',
            '"'+REQUIREMENT+'": {}, "'+REQUIREMENT+'":', 1))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))
