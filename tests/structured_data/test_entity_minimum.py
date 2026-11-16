"""Required entity presence uses actual extruct identities, not entity totals."""

import copy
import json

from qawrench.structured_data import ParityError, compare_files, make_baseline
from . import test_baseline
from .test_parity import BASE, FixtureCase, document


class EntityMinimumTests(FixtureCase):
    run_cli = test_baseline.BaselineTests.run_cli

    def setUp(self):
        super().setUp()
        self.baseline = self.directory / 'accepted.json'
        self.required = 'https://fixture.test/product'

    def test_unrelated_entity_cannot_satisfy_initial_requirement(self):
        text = document({'@id': 'https://fixture.test/other'})
        report = self.compare(text, text, syntaxes=('json-ld',))
        make_baseline(report, minimum_coverage={'json-ld.entities': 1})
        code, out, err = self.run_cli('--syntax', 'json-ld', '--save-baseline', str(self.baseline),
                                      '--require-jsonld-entity', self.required)
        self.assertEqual(code, 2)
        self.assertTrue(out, err)
        error = json.loads(out)['errors'][0]
        self.assertEqual(error['code'], 'minimum_coverage')
        self.assertFalse(self.baseline.exists())
        self.assertEqual([p['capture'] for p in error['provenance']], ['old', 'new'])
        for source in error['provenance']:
            self.assertEqual(source['unmet'], [{'entity': self.required, 'required': 1, 'observed': 0}])
            self.assertEqual(source['coverage']['json-ld']['entities'], 1)
            self.assertEqual(source['coverage']['json-ld']['entity_ids'], ['https://fixture.test/other'])
            self.assertIn('selector', source['coverage']['json-ld'])

    def test_save_replay_and_same_total_replacement(self):
        left = document({'@id': '../product', 'name': 'A'})
        self.compare(left, left.replace('"A"', '"B"'))
        code, out, _ = self.run_cli('--save-baseline', str(self.baseline), '--fail-on-diff',
                                  '--require-jsonld-entity', self.required,
                                  '--minimum-coverage', 'json-ld.entities', '1')
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)['change_count'], 1)
        saved = json.loads(self.baseline.read_text())
        self.assertEqual(saved['required_jsonld_entities'], [self.required])
        self.assertNotIn('required_jsonld_entities', saved['policy'])
        code, out, _ = self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['accepted_change_count'], 1)
        before = self.baseline.read_bytes()
        self.new.write_text(document({'@id': 'https://fixture.test/other'}))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual(code, 2)
        error = json.loads(out)['errors'][0]
        self.assertEqual(error['code'], 'minimum_coverage')
        self.assertEqual([p['capture'] for p in error['provenance']], ['new'])
        self.assertEqual(error['provenance'][0]['unmet'],
                         [{'entity': self.required, 'required': 1, 'observed': 0}])
        self.assertEqual(self.baseline.read_bytes(), before)

    def test_both_initial_captures_and_output_preserve_baseline(self):
        wanted = document({'@id': self.required})
        other = document({'@id': 'https://fixture.test/other'})
        self.compare(wanted, wanted)
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline))[0], 0)
        before = self.baseline.read_bytes()
        output = self.directory / 'error.json'
        for left, right, failed in [(other, wanted, 'old'), (wanted, other, 'new')]:
            with self.subTest(failed=failed):
                self.compare(left, right)
                code, _, _ = self.run_cli('--save-baseline', str(self.baseline), '--out', str(output),
                                          '--require-jsonld-entity', self.required)
                self.assertEqual(code, 2)
                report = json.loads(output.read_text())
                self.assertEqual((report['status'], report['changes']), ('error', []))
                self.assertEqual(report['errors'][0]['code'], 'minimum_coverage')
                self.assertEqual([p['capture'] for p in report['errors'][0]['provenance']], [failed])
                self.assertEqual(self.baseline.read_bytes(), before)

    def test_resolved_id_blank_nodes_and_exact_custom_identity_keys(self):
        text = document([{'@id': '../product'}, {'@id': '_:node'}, {'@id': '#item'}])
        report = self.compare(text, text, syntaxes=('json-ld',))
        requirements = [self.required, '_:node', BASE + '#item']
        saved = make_baseline(report, required_jsonld_entities=requirements)
        self.assertEqual(saved['required_jsonld_entities'], sorted(requirements))
        requirements.append('absent')
        self.assertEqual(len(saved['required_jsonld_entities']), 3)
        for identity in ['../product', '#item', self.required.upper()]:
            with self.subTest(identity=identity), self.assertRaises(ParityError) as raised:
                make_baseline(report, required_jsonld_entities=[identity])
            self.assertEqual(raised.exception.code, 'minimum_coverage')
        self.baseline.write_text(json.dumps(saved))
        self.assertEqual(self.run_cli('--syntax', 'json-ld', '--baseline', str(self.baseline))[0], 0)
        custom = document({'sku': ' SKU 12 ', '@id': '../other'})
        report = self.compare(custom, custom, identity_field='sku')
        saved = make_baseline(report, required_jsonld_entities=[' SKU 12 '])
        self.baseline.write_text(json.dumps(saved))
        self.assertEqual(self.run_cli('--identity-field', 'sku', '--baseline', str(self.baseline))[0], 0)
        for identity in ['SKU 12', 'https://fixture.test/other']:
            with self.subTest(identity=identity), self.assertRaises(ParityError) as raised:
                make_baseline(report, required_jsonld_entities=[identity])
            self.assertEqual(raised.exception.code, 'minimum_coverage')

    def test_skipped_type_and_graph_wrappers_do_not_count_as_entities(self):
        for text in [document({'@id': self.required}).replace('application/ld+json', 'Application/LD+JSON'),
                     document({'@graph': []})]:
            with self.subTest(text=text):
                report = self.compare(text, text)
                with self.assertRaises(ParityError) as raised:
                    make_baseline(report, required_jsonld_entities=[self.required])
                self.assertEqual(raised.exception.code, 'minimum_coverage')
                self.assertEqual(raised.exception.provenance[0]['coverage']['json-ld']['entity_ids'], [])

    def test_combined_requirements_have_deterministic_diagnostics(self):
        report = self.compare(document(), document())
        with self.assertRaises(ParityError) as raised:
            make_baseline(report, minimum_coverage={'json-ld.entities': 1},
                          minimum_og_properties={'og:title': 1}, required_jsonld_entities=['z', 'a'])
        self.assertEqual(raised.exception.provenance[0]['unmet'], [
            {'metric': 'json-ld.entities', 'required': 1, 'observed': 0},
            {'property': 'og:title', 'required': 1, 'observed': 0},
            {'entity': 'a', 'required': 1, 'observed': 0},
            {'entity': 'z', 'required': 1, 'observed': 0}])

    def test_invalid_library_requirements(self):
        report = self.compare(document(), document())
        for requirements in ['id', {}, (), 1, True, [None], [1], [''], [' \n'], ['a', 'a']]:
            with self.subTest(requirements=requirements), self.assertRaises(ParityError) as raised:
                make_baseline(report, required_jsonld_entities=requirements)
            self.assertEqual(raised.exception.code, 'invalid_config')
        only = self.compare(document(), document(), syntaxes=('opengraph',))
        with self.assertRaises(ParityError) as raised:
            make_baseline(only, required_jsonld_entities=['id'])
        self.assertEqual(raised.exception.code, 'invalid_config')

    def test_invalid_cli_requirements_preserve_baseline(self):
        self.compare(document(), document())
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline))[0], 0)
        before = self.baseline.read_bytes()
        cases = [('--require-jsonld-entity', identity) for identity in ['', ' \t']]
        cases += [('--require-jsonld-entity', 'a', '--require-jsonld-entity', 'a'),
                  ('--syntax', 'opengraph', '--require-jsonld-entity', 'a')]
        for args in cases:
            with self.subTest(args=args):
                code, out, _ = self.run_cli('--save-baseline', str(self.baseline), *args)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(out)['errors'][0]['code'], 'invalid_config')
                self.assertEqual(self.baseline.read_bytes(), before)
        for args in [(), ('--baseline', str(self.baseline))]:
            code, out, _ = self.run_cli(*args, '--require-jsonld-entity', 'a')
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(out)['errors'][0]['code'], 'invalid_config')
            self.assertEqual(self.baseline.read_bytes(), before)

    def test_legacy_baselines_and_invalid_saved_requirements(self):
        report = self.compare(document(), document())
        legacy = make_baseline(report)
        self.assertNotIn('required_jsonld_entities', legacy)
        self.assertEqual(make_baseline(report, required_jsonld_entities=None), legacy)
        self.assertEqual(make_baseline(report, required_jsonld_entities=[]), legacy)
        for saved in [legacy, dict(legacy, required_jsonld_entities=[])]:
            self.baseline.write_text(json.dumps(saved))
            self.assertEqual(self.run_cli('--baseline', str(self.baseline))[0], 0)
        for requirements in [None, {}, 'id', [''], [' '], [False], ['a', 'a'], ['absent']]:
            with self.subTest(requirements=requirements):
                saved = copy.deepcopy(legacy)
                saved['required_jsonld_entities'] = requirements
                self.baseline.write_text(json.dumps(saved))
                code, out, _ = self.run_cli('--baseline', str(self.baseline))
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(out)['errors'][0]['code'], 'invalid_baseline')
        only = self.compare(document(), document(), syntaxes=('opengraph',))
        saved = make_baseline(only)
        saved['required_jsonld_entities'] = ['id']
        self.baseline.write_text(json.dumps(saved))
        code, out, _ = self.run_cli('--syntax', 'opengraph', '--baseline', str(self.baseline))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)['errors'][0]['code'], 'invalid_baseline')

    def test_markdown_retains_identity_diagnostics(self):
        self.compare(document(), document())
        code, out, _ = self.run_cli('--save-baseline', str(self.baseline), '--format', 'markdown',
                                  '--require-jsonld-entity', self.required)
        self.assertEqual(code, 2)
        for detail in ('minimum_coverage', self.required, 'entity', 'required', 'observed', 'selector'):
            self.assertIn(detail, out)

    def test_unsorted_saved_requirements_replay_with_sorted_diagnostics(self):
        text = document([{'sku': 'z'}, {'sku': 'a'}])
        report = self.compare(text, text, identity_field='sku')
        saved = make_baseline(report, required_jsonld_entities=['z', 'a'])
        saved['required_jsonld_entities'] = ['z', 'a']
        self.baseline.write_text(json.dumps(saved))
        self.new.write_text(document([{'sku': 'b'}, {'sku': 'c'}]))
        with self.assertRaises(ParityError) as raised:
            compare_files(self.old, self.new, base_url=BASE, identity_field='sku', baseline=self.baseline)
        self.assertEqual(raised.exception.code, 'minimum_coverage')
        self.assertEqual([item['entity'] for item in raised.exception.provenance[0]['unmet']], ['a', 'z'])
