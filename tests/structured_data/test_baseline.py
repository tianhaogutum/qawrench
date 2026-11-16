"""Synthetic integration coverage for saved comparisons and selector integrity."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from unittest.mock import patch

import extruct

from qawrench.structured_data import ParityError, compare_files
from qawrench.structured_data.cli import main, markdown_report
from .test_parity import BASE, FixtureCase, document


class CoverageTests(FixtureCase):
    def test_opengraph_silent_loss_is_not_equal(self):
        actual = extruct.extract
        def lost(*args, **kwargs):
            result = actual(*args, **kwargs)
            result['opengraph'] = []
            return result
        self.old.write_text(document(meta='<meta property="og:title" content="A">'))
        self.new.write_bytes(self.old.read_bytes())
        with patch('qawrench.structured_data.core.extruct.extract', side_effect=lost):
            with self.assertRaises(ParityError) as raised:
                compare_files(self.old, self.new, base_url=BASE)
        self.assertEqual(raised.exception.code, 'extraction_failed')
        self.assertEqual(raised.exception.provenance[0]['line'], 1)

    def test_selector_counts_and_skipped_candidates(self):
        text = document({'@id': 'x'}, meta=(
            '<meta property="og:title" content="A">'
            '<meta name="og:title" content="ignored">'
            '<meta property="unknown:title" content="ignored">'
            '<meta property="og:image">'),
            body='<meta property="og:title" content="body">'
                 '<script type="Application/LD+JSON">{"bad":</script>')
        r = self.compare(text, text)
        coverage = r['captures']['old']['coverage']
        self.assertEqual(coverage['json-ld']['selected'], 1)
        self.assertEqual(coverage['json-ld']['candidates'], 2)
        self.assertEqual(coverage['json-ld']['extracted_roots'], 1)
        self.assertEqual(coverage['json-ld']['skipped'][0]['reason'], 'type_mismatch')
        og = coverage['opengraph']
        self.assertEqual((og['candidates'], og['selected'], og['extracted_values']), (5, 1, 1))
        self.assertEqual({x['reason'] for x in og['skipped']},
                         {'name_attribute', 'unknown_namespace', 'missing_content', 'outside_head'})
        self.assertIn('coverage', markdown_report(r))
        only = self.compare(text, text, syntaxes=('opengraph',))
        self.assertEqual(set(only['captures']['old']['coverage']), {'opengraph'})

    def test_custom_namespaces_and_repeated_values_follow_pinned_upstream(self):
        text = document(meta='<meta property="custom:title" content="A">'
                             '<meta property="custom:title" content="B">').replace(
                                 '<html>', '<html prefix="custom: https://fixture.test/ns#">')
        r = self.compare(text, text)
        self.assertEqual(r['captures']['old']['coverage']['opengraph']['selected'], 2)
        self.assertEqual(r['status'], 'equal')


class BaselineTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.baseline = self.directory / 'accepted.json'
        self.left = document({'@id': 'x', 'price': '12.00'})
        self.right = document({'@id': 'x', 'price': '14.00'})

    def run_cli(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([str(self.old), str(self.new), '--base-url', BASE, *extra])
        return code, out.getvalue(), err.getvalue()

    def save(self, left=None, right=None):
        self.old.write_text(self.left if left is None else left)
        self.new.write_text(self.right if right is None else right)
        code, out, err = self.run_cli('--save-baseline', str(self.baseline))
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_saved_exact_deltas_and_hashes_new_noise_not_accepted(self):
        self.save()
        b = json.loads(self.baseline.read_text())
        self.assertEqual(b['captures']['old']['sha256'], hashlib.sha256(self.old.read_bytes()).hexdigest())
        self.assertEqual(b['captures']['new']['sha256'], hashlib.sha256(self.new.read_bytes()).hexdigest())
        self.assertEqual(b['captures']['new']['coverage']['json-ld']['selected'], 1)
        r = compare_files(self.old, self.new, base_url=BASE, baseline=self.baseline)
        self.assertEqual((r['status'], r['change_count'], r['accepted_change_count']), ('equal', 0, 1))
        self.assertEqual(r['observed_change_count'], 1)
        self.assertEqual(r['baseline']['sha256'], hashlib.sha256(self.baseline.read_bytes()).hexdigest())
        # File locations and surrounding HTML can change without invalidating accepted deltas.
        relocated = self.directory / 'later.html'
        relocated.write_text(self.right + '<!-- later capture -->')
        self.assertEqual(compare_files(self.old, relocated, base_url=BASE, baseline=self.baseline)['status'], 'equal')
        self.new.write_text(self.right.replace('14.00', '15.00'))
        code, out, _ = self.run_cli('--baseline', str(self.baseline), '--fail-on-diff')
        self.assertEqual(code, 1)
        r = json.loads(out)
        self.assertEqual((r['accepted_change_count'], r['changes'][0]['new']), (0, '15.00'))
        self.new.write_text(self.left)
        r = compare_files(self.old, self.new, base_url=BASE, baseline=self.baseline)
        self.assertEqual((r['status'], r['accepted_change_count']), ('equal', 0))

    def test_reference_and_policy_must_match(self):
        self.save()
        original = self.old.read_bytes()
        for options in [{'syntaxes': ('json-ld',)}, {'base_url': 'https://other.test/'},
                        {'normalize': {'/price': 'trim'}}]:
            with self.subTest(options=options), self.assertRaises(ParityError) as raised:
                compare_files(self.old, self.new, baseline=self.baseline, **{'base_url': BASE, **options})
            self.assertEqual(raised.exception.code, 'baseline_mismatch')
        self.old.write_bytes(original + b'<!-- changed reference -->')
        with self.assertRaises(ParityError) as raised:
            compare_files(self.old, self.new, base_url=BASE, baseline=self.baseline)
        self.assertEqual(raised.exception.code, 'baseline_mismatch')

    def test_disappeared_empty_block_cannot_be_accepted(self):
        left = document(scripts=['[]'], meta='<meta property="og:title" content="A">')
        self.save(left, left)
        before = self.baseline.read_bytes()
        # No entity or value delta from removal of this valid empty JSON-LD block.
        self.new.write_text(document(meta='<meta property="og:title" content="A">'))
        for flag in ['--baseline', '--save-baseline']:
            code, out, _ = self.run_cli(flag, str(self.baseline))
            self.assertEqual(code, 2)
            error = json.loads(out)['errors'][0]
            self.assertEqual(error['code'], 'coverage_regression')
            self.assertIn('json-ld.selected', error['message'])
            self.assertEqual(self.baseline.read_bytes(), before)
        # Removing OG metadata fails independently too.
        self.new.write_text(document(scripts=['[]']))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual(code, 2)
        self.assertIn('opengraph.selected', json.loads(out)['errors'][0]['message'])

    def test_entity_and_root_loss_even_when_script_count_stays_same(self):
        left = document([{'@id': 'x'}, {'@id': 'y'}])
        self.save(left, left)
        self.new.write_text(document({'@id': 'x'}))
        with self.assertRaises(ParityError) as raised:
            compare_files(self.old, self.new, base_url=BASE, baseline=self.baseline)
        self.assertEqual(raised.exception.code, 'coverage_regression')
        self.assertIn('json-ld.entities', str(raised.exception))

    def test_replacement_cannot_hide_entity_or_property_coverage_loss(self):
        left = document({'@id': 'x'}, meta='<meta property="og:title" content="A">')
        self.save(left, left)
        for right, diagnostic in [
                (document({'@id': 'y'}, meta='<meta property="og:title" content="A">'), 'json-ld.entity:'),
                (document({'@id': 'x'}, meta='<meta property="og:type" content="A">'), 'opengraph.property:og:title')]:
            self.new.write_text(right)
            for flag in ['--baseline', '--save-baseline']:
                code, out, _ = self.run_cli(flag, str(self.baseline))
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(out)['errors'][0]['code'], 'coverage_regression')
                self.assertIn(diagnostic, json.loads(out)['errors'][0]['message'])

    def test_absence_null_types_and_normalization_in_exact_acceptance(self):
        self.old.write_text(document({'@id': 'x', 'price': ' 12.00 ', 'value': None, 'removed': ''}))
        self.new.write_text(document({'@id': 'x', 'price': ' 14.00 ', 'value': '', 'added': None}))
        opts = ('--normalize', '/price', 'trim')
        self.assertEqual(self.run_cli('--save-baseline', str(self.baseline), *opts)[0], 0)
        code, out, _ = self.run_cli('--baseline', str(self.baseline), '--fail-on-diff', *opts)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['accepted_change_count'], 4)
        # Similar values/kinds at the same path are not a wildcard allowance.
        self.new.write_text(document({'@id': 'x', 'price': '14.00', 'value': [], 'added': ''}))
        code, out, _ = self.run_cli('--baseline', str(self.baseline), '--fail-on-diff', *opts)
        self.assertEqual(code, 1)
        self.assertEqual({c['path'] for c in json.loads(out)['changes']}, {'/value', '/added'})
        code, out, _ = self.run_cli('--baseline', str(self.baseline), '--format', 'markdown', *opts)
        self.assertEqual(code, 0)
        self.assertIn('Accepted exact deltas', out)

    def test_bad_baselines_are_structured_errors(self):
        self.save()
        valid = json.loads(self.baseline.read_text())
        variants = [None, {}, {**valid, 'baseline_version': 99},
                    {**valid, 'accepted_changes': 'anything'},
                    {**valid, 'captures': {}},
                    {**valid, 'accepted_changes': [{'syntax': 'json-ld'}]}]
        for value in variants:
            with self.subTest(value=value):
                self.baseline.write_text(json.dumps(value))
                code, out, _ = self.run_cli('--baseline', str(self.baseline))
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(out)['errors'][0]['code'], 'invalid_baseline')
        for raw in [b'{bad', b'\xff', b'{"baseline_version":1,"baseline_version":1}']:
            self.baseline.write_bytes(raw)
            self.assertEqual(self.run_cli('--baseline', str(self.baseline))[0], 2)
        self.baseline.unlink()
        self.assertEqual(self.run_cli('--baseline', str(self.baseline))[0], 2)

    def test_loading_hand_edited_coverage_loss_is_rejected(self):
        self.save()
        value = json.loads(self.baseline.read_text())
        value['captures']['new']['coverage']['json-ld'].update(
            selected=0, extracted_roots=0, entities=0, entity_ids=[])
        self.baseline.write_text(json.dumps(value))
        code, out, _ = self.run_cli('--baseline', str(self.baseline))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)['errors'][0]['code'], 'invalid_baseline')

    def test_extraction_failure_never_rewrites_baseline(self):
        self.save()
        before = self.baseline.read_bytes()
        self.new.write_text(document(scripts=['{"broken":']))
        for flag in ['--baseline', '--save-baseline']:
            code, out, _ = self.run_cli(flag, str(self.baseline))
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(out)['errors'][0]['code'], 'invalid_jsonld')
            self.assertEqual(self.baseline.read_bytes(), before)

    def test_baseline_output_conflicts_and_failed_atomic_save(self):
        self.save()
        before = self.baseline.read_bytes()
        for path in [self.old, self.new]:
            original = path.read_bytes()
            code, out, _ = self.run_cli('--save-baseline', str(path))
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(out)['errors'][0]['code'], 'output_conflict')
            self.assertEqual(path.read_bytes(), original)
        for flag in ['--baseline', '--save-baseline']:
            code, out, _ = self.run_cli(flag, str(self.baseline), '--out', str(self.baseline))
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(out)['errors'][0]['code'], 'output_conflict')
            self.assertEqual(self.baseline.read_bytes(), before)
        alias = self.directory / 'alias'
        alias.symlink_to(self.old)
        self.assertEqual(self.run_cli('--save-baseline', str(alias))[0], 2)
        with patch('qawrench.structured_data.cli.os.replace', side_effect=OSError('synthetic failure')):
            self.assertEqual(self.run_cli('--save-baseline', str(self.baseline))[0], 2)
        self.assertEqual(self.baseline.read_bytes(), before)
        self.assertEqual(list(self.directory.glob('.parity-*')), [])
