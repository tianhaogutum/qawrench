"""Explicit subset constraints on original top-level JSON-LD array strings."""
import copy
import json

from qawrench.structured_data import ParityError, make_baseline
from qawrench.structured_data.baseline import apply_baseline
from . import test_baseline
from .test_parity import FixtureCase, document

FIELD = 'entity_array_nonblank_string_members'
REQUIREMENT = 'allowed_jsonld_array_strings'
FLAG = '--allow-jsonld-array-string'


class JsonldArrayAllowedTests(FixtureCase):
    run_cli = test_baseline.BaselineTests.run_cli

    def setUp(self):
        super().setUp()
        self.baseline = self.directory / 'accepted.json'
        self.identity = 'https://fixture.test/product'
        self.value = {'@id': '../product', '@type': 'Product', 'name': 'Mug'}
        self.requirements = {self.identity: {'image': ['front.png', 'back.png']}}
        self.flags = (FLAG, self.identity, 'image', 'front.png', FLAG, self.identity, 'image', 'back.png')

    def save(self, report, requirements=None, **kwargs):
        return make_baseline(report, allowed_jsonld_array_strings=(
            self.requirements if requirements is None else requirements), **kwargs)

    def test_required_members_and_floors_do_not_exclude_unexpected_member(self):
        text = document(dict(self.value, image=['front.png', 'back.png', 'side.png', 'side.png']))
        report = self.compare(text, text)
        make_baseline(report, required_jsonld_array_strings=self.requirements,
                      minimum_jsonld_array_distinct_nonblank_strings={self.identity: {'image': 2}})
        code, out, err = self.run_cli('--save-baseline', str(self.baseline), *self.flags)
        self.assertEqual(code, 2)
        self.assertTrue(out, err)
        error = json.loads(out)['errors'][0]
        self.assertEqual(error['code'], 'minimum_coverage')
        self.assertFalse(self.baseline.exists())
        self.assertEqual([p['capture'] for p in error['provenance']], ['old', 'new'])
        for p in error['provenance']:
            self.assertEqual(p['unmet'], [{'entity': self.identity, 'property': 'image',
                'value_filter': 'array-allowed-string-members', 'required': 0, 'observed': 1,
                'observed_type': 'array', 'unexpected_members': ['side.png'],
                'allowed_members': ['back.png', 'front.png'],
                'observed_members': ['back.png', 'front.png', 'side.png']}])
            self.assertIn('selector', p['coverage']['json-ld'])

    def test_subsets_empty_mixed_and_separate_required_count_guards(self):
        for values in [[], ['front.png'], ['front.png', 'back.png', 'back.png'],
                       ['', ' \t\n\u00a0', None, 1, True, {}, ['side.png'], {'@value': 'side.png'}]]:
            with self.subTest(values=values):
                text = document(dict(self.value, image=values))
                report = self.compare(text, text)
                req = copy.deepcopy(self.requirements)
                saved = self.save(report, req)
                req[self.identity]['image'].append('mutation')
                self.assertEqual(saved[REQUIREMENT], {self.identity: {'image': ['back.png', 'front.png']}})
                self.assertNotIn(REQUIREMENT, saved['policy'])
                self.baseline.write_text(json.dumps(saved))
                self.assertEqual(self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')[0], 0)
        text = document(dict(self.value, image=[]))
        report = self.compare(text, text)
        for kwargs in [dict(required_jsonld_array_strings=self.requirements),
                       dict(required_jsonld_nonempty_arrays={self.identity: ['image']}),
                       dict(minimum_jsonld_array_nonblank_strings={self.identity: {'image': 1}}),
                       dict(minimum_jsonld_array_distinct_nonblank_strings={self.identity: {'image': 1}})]:
            with self.assertRaises(ParityError) as raised: self.save(report, **kwargs)
            self.assertEqual(raised.exception.code, 'minimum_coverage')

    def test_full_original_values_before_normalization_no_coercion(self):
        values = [' A ', 'A', 'a', 'é', 'e\u0301', '\u200b', 'not a URL', 'back.png']
        for value in values:
            text = document(dict(self.value, image=[value, value, None, ['ignored']]))
            report = self.compare(text, text, set_valued=('/image',))
            self.save(report, {self.identity: {'image': [value]}})
            if value != 'back.png':
                with self.assertRaises(ParityError) as raised: self.save(report)
                self.assertEqual(raised.exception.provenance[0]['unmet'][0]['unexpected_members'], [value])
            else:
                self.save(report)
        for mode, left, right in [('trim', ' A ', 'A'), ('casefold', 'A', 'a')]:
            report = self.compare(document(dict(self.value, image=[left])), document(dict(self.value, image=[right])),
                                  normalize={'/image/*': mode}, set_valued=('/image',))
            self.assertEqual(report['changes'], [])
            with self.assertRaises(ParityError) as raised: self.save(report, {self.identity: {'image': [right]}})
            self.assertEqual([p['capture'] for p in raised.exception.provenance], ['old'])
        text = document(dict(self.value, image=[1, True, ['side.png'], {'@value': 'side.png'}]))
        self.save(self.compare(text, text))

    def test_exact_literal_identity_property_no_expansion(self):
        for value in [dict(self.value, Image=['front.png'], nested={'image': ['front.png']}),
                      dict(self.value, picture=['front.png'], **{'@context': {'picture': 'image'}}),
                      {'@id': '../other', 'image': ['front.png']}, {'@graph': []}]:
            with self.subTest(value=value), self.assertRaises(ParityError) as raised:
                self.save(self.compare(document(value), document(value)))
            self.assertEqual(raised.exception.code, 'minimum_coverage')
        text = document(dict(self.value, image=['front.png'])).replace('application/ld+json', 'Application/LD+JSON')
        with self.assertRaises(ParityError): self.save(self.compare(text, text))
        text = document({'@graph': [dict(self.value, **{' /image~* ': [' back.png ']}),
                                    {'@id': '_:node', 'image': []}]})
        report = self.compare(text, text)
        self.save(report, {self.identity: {' /image~* ': [' back.png ']}, '_:node': {'image': ['x']}})
        for identity in ['../product', self.identity.upper()]:
            with self.assertRaises(ParityError): self.save(report, {identity: {' /image~* ': [' back.png ']}})
        text = document({'sku': ' SKU 12 ', 'image': []})
        report = self.compare(text, text, identity_field='sku')
        self.save(report, {' SKU 12 ': {'image': ['x']}})
        with self.assertRaises(ParityError): self.save(report, {'SKU 12': {'image': ['x']}})

    def test_absent_nonarray_both_initial_sides_json_markdown_preserve(self):
        good = document(dict(self.value, image=['front.png', 'back.png']))
        self.compare(good, good)
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline), *self.flags)[0], 0)
        before = self.baseline.read_bytes()
        for value in [None, '', 'front.png', {}, {'@list': ['front.png']}, 1, False,
                      ['front.png', 'side.png', 'placeholder.png', 'side.png'], 'missing']:
            bad = document(self.value if value == 'missing' else dict(self.value, image=value))
            for left, right, side, fmt in [(bad, good, 'old', 'json'), (good, bad, 'new', 'markdown')]:
                with self.subTest(value=value, side=side):
                    self.compare(left, right)
                    code, out, _ = self.run_cli('--save-baseline', str(self.baseline), '--format', fmt, *self.flags)
                    self.assertEqual(code, 2)
                    for detail in ['minimum_coverage', self.identity, 'image', 'unexpected_members',
                                   'allowed_members', 'observed_type', 'selector', 'array-allowed-string-members']:
                        self.assertIn(detail, out)
                    if fmt == 'json':
                        r = json.loads(out)
                        self.assertEqual((r['status'], r['changes']), ('error', []))
                        self.assertEqual([p['capture'] for p in r['errors'][0]['provenance']], [side])
                    self.assertEqual(self.baseline.read_bytes(), before)

    def test_replay_before_accepted_deltas_both_current_sides_and_earlier_guards(self):
        good = document(dict(self.value, image=['front.png', 'back.png'], sameAs=['other']))
        report = self.compare(good, good.replace('Mug', 'Cup'))
        saved = self.save(report, {self.identity: {'image': ['back.png', 'front.png'], 'sameAs': ['other']}},
                          required_jsonld_array_strings=self.requirements,
                          minimum_jsonld_array_distinct_nonblank_strings={self.identity: {'image': 2}},
                          minimum_jsonld_array_nonblank_strings={self.identity: {'image': 2}},
                          required_jsonld_entities=[self.identity], required_jsonld_types={self.identity: ['Product']},
                          required_jsonld_nonblank_properties={self.identity: ['name']},
                          required_jsonld_nonempty_arrays={self.identity: ['image']})
        self.baseline.write_text(json.dumps(saved))
        self.assertEqual(self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')[0], 0)
        # Still meets required members and floors; accepted exact added delta cannot bypass allowlist.
        saved['accepted_changes'].append(dict(syntax='json-ld', scope='entity', entity=self.identity,
            kind='added', path='/image/2', new='side.png'))
        self.baseline.write_text(json.dumps(saved)); before = self.baseline.read_bytes()
        self.new.write_text(good.replace('Mug', 'Cup').replace('"back.png"', '"back.png", "side.png"'))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        r = json.loads(out)
        self.assertEqual((code, r['errors'][0]['code'], r['changes']), (2, 'minimum_coverage', []))
        self.assertEqual([p['capture'] for p in r['errors'][0]['provenance']], ['new'])
        self.assertEqual(self.baseline.read_bytes(), before)
        report['captures']['old']['coverage']['json-ld'][FIELD][self.identity]['image'].append('side.png')
        with self.assertRaises(ParityError) as raised: apply_baseline(report, self.baseline)
        self.assertEqual(raised.exception.code, 'minimum_coverage')
        self.assertEqual([p['capture'] for p in raised.exception.provenance], ['old'])
        self.compare(good, good.replace('Mug', 'Cup'))
        code, out, _ = self.run_cli('--baseline', str(self.baseline), '--set-valued', '/image')
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'baseline_mismatch'))
        self.old.write_text(good+' ')
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'baseline_mismatch'))
        # No implicit length floor; an explicit allowlist still requires array shape.
        report = self.compare(good, document(dict(self.value, image=['front.png'])))
        self.assertEqual(self.save(report)['captures']['new']['coverage']['json-ld'][FIELD][self.identity]['image'], ['front.png'])
        self.baseline.write_text(json.dumps(self.save(self.compare(good, good))))
        self.new.write_text(document(dict(self.value, image='front.png')))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'minimum_coverage'))

    def test_invalid_library_cli_and_modes(self):
        report = self.compare(document(), document())
        invalid = [[], 'id', {1: {'image': ['x']}}, {' ': {'image': ['x']}}, {'id': {}}, {'id': []},
                   {'id': {'': ['x']}}, {'id': {' ': ['x']}}, {'id': {1: ['x']}}]
        invalid += [{'id': {'image': v}} for v in [None, [], {}, 'x', 1, True, [1], [None], [''], [' \t'], ['x', 'x']]]
        for req in invalid:
            with self.subTest(req=req), self.assertRaises(ParityError) as raised: self.save(report, req)
            self.assertEqual(raised.exception.code, 'invalid_config')
        only = self.compare(document(), document(), syntaxes=('opengraph',))
        with self.assertRaises(ParityError) as raised: self.save(only)
        self.assertEqual(raised.exception.code, 'invalid_config')
        self.assertNotIn(REQUIREMENT, self.save(only, {}))
        self.compare(document(), document())
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline))[0], 0)
        before = self.baseline.read_bytes()
        flags = [(FLAG, *triple) for triple in [('', 'image', 'x'), (' ', 'image', 'x'), ('id', '', 'x'),
                 ('id', ' ', 'x'), ('id', 'image', ''), ('id', 'image', ' \t')]]
        flags += [(*self.flags, *self.flags), ('--syntax', 'opengraph', *self.flags)]
        for args in flags:
            code, out, _ = self.run_cli('--save-baseline', str(self.baseline), *args)
            self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_config'))
            self.assertEqual(self.baseline.read_bytes(), before)
        for args in [(), ('--baseline', str(self.baseline))]:
            code, out, _ = self.run_cli(*args, *self.flags)
            self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_config'))

    def test_historical_no_inference_and_no_implicit_allowlist(self):
        text = document(dict(self.value, image=['side.png']))
        report = self.compare(text, text)
        for historical in [False, True]:
            if historical:
                for c in report['captures'].values(): c['coverage']['json-ld'].pop(FIELD)
            self.baseline.write_text(json.dumps(make_baseline(report)))
            self.assertEqual(self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')[0], 0)
        with self.assertRaises(ParityError) as raised: self.save(report)
        self.assertEqual(raised.exception.code, 'invalid_config')
        saved = make_baseline(report); saved[REQUIREMENT] = self.requirements
        self.baseline.write_text(json.dumps(saved))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))

    def test_saved_requirements_metadata_and_saved_shortfalls(self):
        text = document(dict(self.value, image=['front.png', 'back.png', None]))
        valid = self.save(self.compare(text, text))
        variants = []
        for req in [None, [], {'id': {}}, {'': {'image': ['back.png']}}, {'id': {'image': []}},
                    {'id': {'image': ['x', 'x']}}, {self.identity: {'image': ['front.png']}}]:
            saved = copy.deepcopy(valid); saved[REQUIREMENT] = req; variants.append(saved)
        for field in [FIELD, 'entity_property_shapes']:
            saved = copy.deepcopy(valid); del saved['captures']['new']['coverage']['json-ld'][field]; variants.append(saved)
        for members in [None, [], {}, {'other': {}}, {self.identity: []}, {self.identity: {}},
                        {self.identity: {'name': []}}, *[{self.identity: {'image': v}} for v in
                            [None, 'x', {}, [True], [1], [''], [' '], ['back.png', 'back.png'], [],
                             ['back.png', 'extra'], ['1', '2', '3', '4']]]]:
            saved = copy.deepcopy(valid); saved['captures']['new']['coverage']['json-ld'][FIELD] = members; variants.append(saved)
        only = make_baseline(self.compare(document(), document(), syntaxes=('opengraph',)))
        only[REQUIREMENT] = self.requirements
        for saved in variants + [only]:
            self.baseline.write_text(json.dumps(saved)); before = self.baseline.read_bytes()
            code, out, _ = self.run_cli('--baseline', str(self.baseline))
            self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))
            self.assertEqual(self.baseline.read_bytes(), before)
        self.baseline.write_text(json.dumps(valid).replace('"allowed_jsonld_array_strings":',
            '"allowed_jsonld_array_strings": {}, "allowed_jsonld_array_strings":', 1))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))
