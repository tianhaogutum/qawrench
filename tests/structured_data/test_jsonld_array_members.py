"""Required exact original immediate nonblank strings in top-level arrays."""
import copy
import json

from qawrench.structured_data import ParityError, make_baseline
from . import test_baseline
from .test_parity import FixtureCase, document

FIELD = 'entity_array_nonblank_string_members'
REQUIREMENT = 'required_jsonld_array_strings'
FLAG = '--require-jsonld-array-string'


class JsonldArrayMemberTests(FixtureCase):
    run_cli = test_baseline.BaselineTests.run_cli

    def setUp(self):
        super().setUp()
        self.baseline = self.directory / 'accepted.json'
        self.identity = 'https://fixture.test/product'
        self.value = {'@id': '../product', '@type': 'Product', 'name': 'Mug'}
        self.requirements = {self.identity: {'image': ['back.png']}}
        self.flags = (FLAG, self.identity, 'image', 'back.png')

    def save(self, report, requirements=None):
        return make_baseline(report, required_jsonld_array_strings=(
            self.requirements if requirements is None else requirements))

    def test_arbitrary_distinct_strings_cannot_substitute_required_member(self):
        text = document(dict(self.value, image=['front.png', 'side.png']))
        report = self.compare(text, text)
        make_baseline(report, minimum_jsonld_array_distinct_nonblank_strings={self.identity: {'image': 2}})
        code, out, err = self.run_cli('--save-baseline', str(self.baseline), *self.flags)
        self.assertEqual(code, 2)
        self.assertTrue(out, err)
        error = json.loads(out)['errors'][0]
        self.assertEqual(error['code'], 'minimum_coverage')
        self.assertFalse(self.baseline.exists())
        self.assertEqual([p['capture'] for p in error['provenance']], ['old', 'new'])
        for p in error['provenance']:
            self.assertEqual(p['unmet'], [{'entity': self.identity, 'property': 'image', 'member': 'back.png',
                'value_filter': 'array-string-member', 'required': 1, 'observed': 0, 'observed_type': 'array',
                'observed_members': ['front.png', 'side.png']}])
            self.assertIn('selector', p['coverage']['json-ld'])

    def test_immediate_original_members_and_conjunctive_exact_matching(self):
        values = ['front.png', 'back.png', 'back.png', ' A ', 'A', 'a', 'é', 'e\u0301', '\u200b',
                  'not a URL', '', ' \t\n\u00a0', None, 1, True, {}, ['nested'], {'@value': 'object'}]
        text = document(dict(self.value, image=values))
        report = self.compare(text, text, set_valued=('/image',))
        expected = sorted({v for v in values if isinstance(v, str) and v.strip()})
        self.assertEqual(report['captures']['old']['coverage']['json-ld'][FIELD], {self.identity: {'image': expected}})
        req = {self.identity: {'image': expected.copy()}}
        saved = self.save(report, req)
        req[self.identity]['image'].append('new')
        self.assertEqual(saved[REQUIREMENT], {self.identity: {'image': expected}})
        for member in ['nested', 'object', '1', 'true', 'BACK.PNG', ' back.png ', 'absent']:
            with self.subTest(member=member), self.assertRaises(ParityError) as raised:
                self.save(report, {self.identity: {'image': ['front.png', member]}})
            self.assertEqual(raised.exception.code, 'minimum_coverage')
            self.assertEqual(raised.exception.provenance[0]['unmet'][0]['member'], member)
        for mode, old_values, new_values, member in [
                ('trim', [' A ', 'A'], ['A', 'A'], ' A '),
                ('casefold', ['A', 'a'], ['a', 'a'], 'A')]:
            report = self.compare(document(dict(self.value, image=old_values)),
                                  document(dict(self.value, image=new_values)),
                                  set_valued=('/image',), normalize={'/image/*': mode})
            self.assertEqual(report['changes'], [])
            with self.assertRaises(ParityError) as raised:
                self.save(report, {self.identity: {'image': [member]}})
            self.assertEqual([p['capture'] for p in raised.exception.provenance], ['new'])

    def test_literal_property_identity_and_no_expansion(self):
        for value in [dict(self.value, Image=['back.png'], nested={'image': ['back.png']}),
                      dict(self.value, picture=['back.png'], **{'@context': {'picture': 'image'}}),
                      {'@id': '../other', 'image': ['back.png']}, {'@graph': []}]:
            with self.subTest(value=value), self.assertRaises(ParityError) as raised:
                self.save(self.compare(document(value), document(value)))
            self.assertEqual(raised.exception.code, 'minimum_coverage')
        text = document(dict(self.value, image=['back.png'])).replace('application/ld+json', 'Application/LD+JSON')
        with self.assertRaises(ParityError): self.save(self.compare(text, text))
        text = document({'@graph': [dict(self.value, **{' /image~* ': [' back.png ']}),
                                    {'@id': '_:node', 'image': ['back.png']}]})
        report = self.compare(text, text)
        req = {self.identity: {' /image~* ': [' back.png ']}, '_:node': {'image': ['back.png']}}
        self.baseline.write_text(json.dumps(self.save(report, req)))
        self.assertEqual(self.run_cli('--baseline', str(self.baseline))[0], 0)
        for identity in ['../product', self.identity.upper()]:
            with self.assertRaises(ParityError): self.save(report, {identity: {' /image~* ': [' back.png ']}})
        text = document({'sku': ' SKU 12 ', 'image': ['back.png']})
        report = self.compare(text, text, identity_field='sku')
        self.baseline.write_text(json.dumps(self.save(report, {' SKU 12 ': {'image': ['back.png']}})))
        self.assertEqual(self.run_cli('--identity-field', 'sku', '--baseline', str(self.baseline))[0], 0)
        with self.assertRaises(ParityError): self.save(report, {'SKU 12': {'image': ['back.png']}})

    def test_absent_nonarray_and_both_initial_sides_preserve_bytes(self):
        good = document(dict(self.value, image=['front.png', 'back.png']))
        self.compare(good, good)
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline), *self.flags)[0], 0)
        before = self.baseline.read_bytes()
        bad_values = [None, '', 'back.png', {}, {'@list': ['back.png']}, 1, False, [],
                      [None, '', ' ', ['back.png'], {'@value': 'back.png'}], ['front.png', 'side.png']]
        for value in bad_values + ['missing']:
            bad = document(self.value if value == 'missing' else dict(self.value, image=value))
            for left, right, side, fmt in [(bad, good, 'old', 'json'), (good, bad, 'new', 'markdown')]:
                with self.subTest(value=value, side=side):
                    self.compare(left, right)
                    code, out, _ = self.run_cli('--save-baseline', str(self.baseline), '--format', fmt, *self.flags)
                    self.assertEqual(code, 2)
                    for detail in ['minimum_coverage', self.identity, 'image', 'back.png', 'observed_members',
                                   'observed_type', 'selector', 'array-string-member']:
                        self.assertIn(detail, out)
                    if fmt == 'json':
                        r = json.loads(out)
                        self.assertEqual((r['status'], r['changes']), ('error', []))
                        self.assertEqual([p['capture'] for p in r['errors'][0]['provenance']], [side])
                    self.assertEqual(self.baseline.read_bytes(), before)

    def test_save_replay_all_guards_and_members_before_delta_acceptance(self):
        from qawrench.structured_data.baseline import apply_baseline
        good = document(dict(self.value, image=['front.png', 'back.png'], sameAs=['other'], description='Cup'),
                        meta='<meta property="og:title" content="Mug">')
        self.compare(good, good.replace('Cup', 'Glass'))
        flags = (*self.flags, FLAG, self.identity, 'image', 'front.png', FLAG, self.identity, 'sameAs', 'other',
                 '--require-jsonld-entity', self.identity, '--require-jsonld-type', self.identity, 'Product',
                 '--require-jsonld-nonblank-property', self.identity, 'name',
                 '--require-jsonld-nonempty-array', self.identity, 'image',
                 '--minimum-jsonld-array-nonblank-string', self.identity, 'image', '2',
                 '--minimum-jsonld-array-distinct-nonblank-string', self.identity, 'image', '2',
                 '--minimum-coverage', 'json-ld.entities', '1', '--minimum-og-property', 'og:title', '1',
                 '--minimum-og-nonblank-property', 'og:title', '1')
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline), '--fail-on-diff', *flags)[0], 1)
        saved = json.loads(self.baseline.read_text())
        self.assertEqual(saved[REQUIREMENT], {self.identity: {'image': ['back.png', 'front.png'], 'sameAs': ['other']}})
        self.assertNotIn(REQUIREMENT, saved['policy'])
        self.assertEqual(self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')[0], 0)
        # Equal counts/shapes and a manually accepted exact delta cannot hide missing membership.
        saved['accepted_changes'].append(dict(syntax='json-ld', scope='entity', entity=self.identity,
            kind='changed', path='/image/1', old='back.png', new='side.png'))
        self.baseline.write_text(json.dumps(saved)); before = self.baseline.read_bytes()
        self.new.write_text(good.replace('Cup', 'Glass').replace('back.png', 'side.png'))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        r = json.loads(out)
        self.assertEqual((code, r['errors'][0]['code'], r['changes']), (2, 'minimum_coverage', []))
        self.assertEqual([p['capture'] for p in r['errors'][0]['provenance']], ['new'])
        self.assertEqual(self.baseline.read_bytes(), before)
        report = self.compare(good, good.replace('Cup', 'Glass'))
        report['captures']['old']['coverage']['json-ld'][FIELD][self.identity]['image'] = ['front.png', 'side.png']
        with self.assertRaises(ParityError) as raised: apply_baseline(report, self.baseline)
        self.assertEqual(raised.exception.code, 'minimum_coverage')
        self.assertEqual([p['capture'] for p in raised.exception.provenance], ['old'])
        # Policy and immutable-reference guards remain effective with requirements saved.
        self.compare(good, good.replace('Cup', 'Glass'))
        code, out, _ = self.run_cli('--baseline', str(self.baseline), '--set-valued', '/image')
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'baseline_mismatch'))
        self.old.write_text(good+' ')
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'baseline_mismatch'))

    def test_invalid_configs_modes_and_duplicates(self):
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
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline))[0], 0)
        before = self.baseline.read_bytes()
        args = [(FLAG, *triple) for triple in [('', 'image', 'x'), (' ', 'image', 'x'), ('id', '', 'x'),
                ('id', ' ', 'x'), ('id', 'image', ''), ('id', 'image', ' \t')]]
        args += [(*self.flags, *self.flags), ('--syntax', 'opengraph', *self.flags)]
        for flags in args:
            with self.subTest(flags=flags):
                code, out, _ = self.run_cli('--save-baseline', str(self.baseline), *flags)
                self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_config'))
                self.assertEqual(self.baseline.read_bytes(), before)
        for flags in [(), ('--baseline', str(self.baseline))]:
            code, out, _ = self.run_cli(*flags, *self.flags)
            self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_config'))

    def test_historical_metadata_no_inference_and_no_implicit_membership_floor(self):
        report = self.compare(document(dict(self.value, image=['back.png'])),
                              document(dict(self.value, image=['side.png'])))
        for historical in [False, True]:
            if historical:
                for c in report['captures'].values(): c['coverage']['json-ld'].pop(FIELD)
            self.baseline.write_text(json.dumps(make_baseline(report)))
            self.assertEqual(self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')[0], 0)
        # Even a no-delta report with the right count cannot reconstruct original strings.
        text = document(dict(self.value, image=['back.png']))
        report = self.compare(text, text)
        for c in report['captures'].values(): c['coverage']['json-ld'].pop(FIELD)
        with self.assertRaises(ParityError) as raised: self.save(report)
        self.assertEqual(raised.exception.code, 'invalid_config')
        saved = make_baseline(report); saved[REQUIREMENT] = self.requirements
        self.baseline.write_text(json.dumps(saved))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))
        make_baseline(report, minimum_jsonld_array_distinct_nonblank_strings={self.identity: {'image': 1}})

    def test_saved_metadata_schema_counts_shapes_and_shortfalls(self):
        text = document(dict(self.value, image=['back.png', None, None]))
        valid = self.save(self.compare(text, text))
        variants = []
        for req in [None, [], {'id': {}}, {'': {'image': ['back.png']}}, {'id': {'image': []}},
                    {'id': {'image': ['x', 'x']}}, {self.identity: {'image': ['missing']}}]:
            saved = copy.deepcopy(valid); saved[REQUIREMENT] = req; variants.append(saved)
        for members in [None, [], {}, {'other': {}}, {self.identity: []}, {self.identity: {}},
                        {self.identity: {'name': []}}, *[{self.identity: {'image': v}} for v in
                            [None, 'x', {}, [True], [1], [''], [' '], ['back.png', 'back.png'], [],
                             ['back.png', 'extra'], ['1', '2', '3', '4'], ['side.png']]]]:
            saved = copy.deepcopy(valid); saved['captures']['new']['coverage']['json-ld'][FIELD] = members; variants.append(saved)
        for field in [FIELD, 'entity_property_shapes']:
            saved = copy.deepcopy(valid); del saved['captures']['new']['coverage']['json-ld'][field]; variants.append(saved)
        # Independently validate membership against shapes and available multiplicity.
        for values, length, total in [(['a','b'], 1, 2), (['a','b'], 3, 1), ([], 3, 1), (['a'], 3, 0)]:
            saved = copy.deepcopy(valid); cov = saved['captures']['new']['coverage']['json-ld']
            cov.pop('entity_array_distinct_nonblank_string_counts')
            cov[FIELD][self.identity]['image'] = values
            cov['entity_property_shapes'][self.identity]['image']['length'] = length
            cov['entity_array_nonblank_string_counts'][self.identity]['image'] = total
            variants.append(saved)
        only = make_baseline(self.compare(document(), document(), syntaxes=('opengraph',)))
        only[REQUIREMENT] = self.requirements
        for saved in variants + [only]:
            with self.subTest(saved=saved):
                self.baseline.write_text(json.dumps(saved)); before = self.baseline.read_bytes()
                code, out, _ = self.run_cli('--baseline', str(self.baseline))
                self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))
                self.assertEqual(self.baseline.read_bytes(), before)
        self.baseline.write_text(json.dumps(valid).replace('"image": ["back.png"]', '"image": ["back.png"], "image": ["back.png"]', 1))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual((code, json.loads(out)['errors'][0]['code']), (2, 'invalid_baseline'))
